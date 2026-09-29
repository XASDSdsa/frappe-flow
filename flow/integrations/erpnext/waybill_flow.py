"""Reviewed Flow entry points for replacing an SF waybill.

The carrier lifecycle remains in :mod:`waybill`; this module only adds a
small, auditable Flow boundary around it.  A replacement is never silently
activated: shipped replacements require external SF support feedback first.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime
from flow.lib.tool import tool

from erpnext_shipping.sf_international import waybill
from .sales_order_flow import _actor as _sales_actor, _scope as _sales_scope

TTL = 1800
SERVICE = "Flow SF Waybill Replacement"


def _actor():
	return _sales_actor()


def _scope():
	return _sales_scope()


def _key(token):
	if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
		frappe.throw("换单方案编号无效，请重新预检。")
	return "flow-sf-waybill-replacement:" + token


def _ledger_name(token):
	if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
		frappe.throw("换单方案编号无效，请重新预检。")
	return "flow-sf-waybill-replacement-" + token


def _load(token):
	plan = frappe.cache.get_value(_key(token))
	if not plan or now_datetime() > get_datetime(plan["expires_at"]):
		frappe.throw("换单方案已过期，请重新预检。")
	if (plan.get("user"), plan.get("scope"), plan.get("site")) != (_actor(), _scope(), frappe.local.site):
		frappe.throw("此换单方案不属于当前账号或对话。")
	return plan


def _json_form(form):
	if isinstance(form, dict):
		value = dict(form)
	else:
		try:
			value = json.loads(form or "{}")
		except (TypeError, ValueError):
			frappe.throw("新面单参数必须是有效的 JSON 对象。")
	if not isinstance(value, dict) or not value:
		frappe.throw("请填写需要替换的寄件、收件或货物参数。")
	return value


def _read_state(shipment, *, doc=None, for_update=False):
	doc = doc or frappe.get_doc("Shipment", shipment, for_update=for_update)
	doc.check_permission("read")
	if not waybill._waybill_doctype_available():
		frappe.throw("顺丰面单历史表不可用，暂时不能办理换单。")
	rows = waybill._records(doc)
	active = [row for row in rows if int(row.get("is_active") or 0) and str(row.get("replacement_status") or "") in {waybill.ACTIVE, "当前"}]
	current = str(waybill._waybill(doc) or "").strip()
	if len(active) > 1:
		frappe.throw("该运单存在多个当前面单，请先核对面单历史。")
	if active:
		candidate = active[0]
	else:
		# Before dispatch, the carrier cancellation workflow keeps the old
		# number as the parent pointer and marks its child row 已取消.  That
		# exact, evidenced row is still the only valid source for a replacement.
		cancelled = [row for row in rows
			if str(row.get("waybill") or "").strip() == current
			and str(row.get("replacement_status") or "").strip() == "已取消"]
		if len(cancelled) != 1:
			candidate = None
		else:
			candidate = cancelled[0]
	if not current or candidate is None or str(candidate.get("waybill") or "").strip() != current:
		frappe.throw("当前运单没有可替换的有效顺丰面单。")
	record = frappe.get_doc("SF Waybill", candidate.name, for_update=for_update)
	record.check_permission("read")
	if record.get("shipment") != doc.name:
		frappe.throw("顺丰面单历史不属于当前运单。")
	if str(record.get("replacement_status") or "").strip() == "已取消" and not waybill._cancel_evidence_source(doc, record):
		frappe.throw("原面单虽标记为已取消，但缺少顺丰明确取消凭证。")
	shipped = bool(waybill._shipment_is_shipped(doc))
	return doc, record, shipped


def _decode_dict(value):
	if isinstance(value, dict):
		return dict(value)
	try:
		value = json.loads(value or "{}")
	except (TypeError, ValueError):
		return {}
	return dict(value) if isinstance(value, dict) else {}


def _canonical_form(doc, record, form):
	"""Match waybill._new_record's immutable sender before showing approval."""
	from erpnext_shipping.sf_international import shipping
	form = dict(form)
	authoritative = dict(shipping._sender_from_warehouse(
		doc=doc, pickup_address_name=doc.get("pickup_address_name")) or {})
	parent_form = _decode_dict(doc.get("sf_form_json"))
	old_form = _decode_dict(record.get("form_payload"))
	for source in (parent_form.get("sender"), old_form.get("sender")):
		if not isinstance(source, dict):
			continue
		for field, value in source.items():
			if value is not None and (not isinstance(value, str) or value.strip()):
				authoritative[field] = value
	form["sender"] = authoritative
	return form


def _json(value):
	return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _existing_execution(token):
	"""Return the durable result for an already approved token, if present."""
	name = _ledger_name(token)
	if not frappe.db.exists("Integration Request", name):
		return None
	ledger = frappe.get_doc("Integration Request", name, for_update=True)
	data = _decode_dict(ledger.data)
	if (ledger.integration_request_service, data.get("user"), data.get("scope"), data.get("site")) != (
		SERVICE, _actor(), _scope(), frappe.local.site):
		frappe.throw("此换单执行记录不属于当前账号或对话。")
	if ledger.status in {"Queued", "Processing"}:
		return {"status": "processing", "verified": False, "preview_token": token,
			"shipment": data.get("shipment"), "message": "本批准方案已开始执行，正在等待上一次结果；没有再次请求顺丰。"}
	result = _decode_dict(ledger.output)
	if result:
		result["idempotent"] = True
		result["message"] = "本批准方案已经执行，已返回原结果；没有再次创建顺丰面单。"
		return result
	return {"status": "error", "verified": False, "preview_token": token,
		"reason": "原执行记录没有保存结果，请核对运单历史后再处理。", "message": "没有再次请求顺丰。"}


def _summary(doc, record, shipped, reason, form):
	return {
		"title": "顺丰面单替换审核",
		"scenario": "已发货后由顺丰客服协助替换" if shipped else "未发货且原面单已取消后重新创建",
		"shipment": doc.name,
		"old_waybill": record.get("waybill"),
		"reason": reason,
		"new_parameters": form,
		"rules": [
			"旧单号和运费历史保留。",
			"新单创建成功不等于已发货。",
			"已发货场景必须记录顺丰外部客服反馈，确认成功后才能启用新单。" if shipped else "未发货场景必须已有原单明确取消凭证。",
			"未发货场景创建成功后会按系统规则自动启用新单。" if not shipped else "已发货场景创建成功后保持待替换，不能自动切换当前单号。",
		],
	}


def _confirmation(args):
	try:
		plan = _load((args or {}).get("preview_token"))
		doc, record, shipped = _read_state(plan["shipment"])
		summary = _summary(doc, record, shipped, plan["reason"], plan["form"])
		lines = ["**顺丰面单替换审核**", "", f"运单：{summary['shipment']}",
			f"原面单：{summary['old_waybill']}", f"场景：{summary['scenario']}",
			f"原因：{summary['reason']}", f"新参数：{json.dumps(summary['new_parameters'], ensure_ascii=False)}", ""]
		lines.extend("⚠️ " + rule for rule in summary["rules"])
		lines.append("核对无误后批准一次；需修改请选择拒绝并重新预检。")
		return "\n".join(lines)
	except Exception as exc:
		return "🔴 换单方案不可执行：" + str(exc)


def preview_sf_waybill_replacement(shipment: str, form: dict | str, reason: str, shipped: bool | None = None):
	"""只读预检替换参数，不联系顺丰、不创建本地记录。"""
	try:
		_actor()
		if not isinstance(reason, str) or len("".join(reason.split())) < 4:
			frappe.throw("请填写具体换单原因（至少四个字符）。")
		doc, record, actual_shipped = _read_state(shipment)
		if shipped is not None and type(shipped) is not bool:
			frappe.throw("shipped 必须是 true 或 false。")
		if shipped is not None and shipped != actual_shipped:
			frappe.throw("换单场景与当前物流状态不一致，请刷新后重新预检。")
		form = _canonical_form(doc, record, _json_form(form))
		# Validate the edited fields before showing the approval card. The carrier
		# request is still made only by the approved create tool.
		from erpnext_shipping.sf_international import shipping
		shipping._create_order_body_from_form(doc, form)
		token = uuid.uuid4().hex
		plan = {"version": 1, "user": _actor(), "scope": _scope(), "site": frappe.local.site,
			"shipment": doc.name, "reason": reason.strip()[:2000], "form": form,
			"shipped": actual_shipped, "old_waybill": record.get("waybill"),
			"expires_at": str(now_datetime() + timedelta(seconds=TTL))}
		frappe.cache.set_value(_key(token), plan, expires_in_sec=TTL)
		return {"status": "preview", "verified": False, "preview_token": token,
			"summary": _summary(doc, record, actual_shipped, plan["reason"], form),
			"reason": "预检完成，尚未创建新面单；请核对审核卡后批准一次。"}
	except Exception as exc:
		return {"status": "needs_input", "verified": False, "reason": str(exc), "message": "未创建新面单。"}


@tool(requires_confirmation=True, confirm_prompt=_confirmation)
def create_sf_waybill_replacement(preview_token: str):
	"""批准并创建替换面单；旧单号保留，未发货自动启用、已发货等待外部确认。"""
	ledger = None
	try:
		# Check the durable ledger before loading the expiring preview cache. A
		# retry after TTL or a new Flow request must still be idempotent.
		existing = _existing_execution(preview_token)
		if existing:
			return existing
		plan = _load(preview_token)
		# Lock the parent and its exact old history row before sending the carrier
		# request. This rejects stale approvals after another process changed the
		# current number, cancellation evidence, or shipped state.
		locked = waybill._shipment(plan["shipment"])
		doc, record, shipped = _read_state(plan["shipment"], doc=locked, for_update=True)
		if str(record.get("waybill") or "").strip() != str(plan.get("old_waybill") or "").strip() or shipped != bool(plan.get("shipped")):
			frappe.throw("批准期间原面单或发货状态已变化，请重新预检。")
		from erpnext_shipping.sf_international import shipping
		form = _canonical_form(doc, record, plan["form"])
		shipping._create_order_body_from_form(doc, form)
		if _json(form) != _json(plan["form"]):
			frappe.throw("批准期间寄件来源或新面单参数已变化，请重新预检。")
		ledger = frappe.get_doc({"doctype": "Integration Request",
			"integration_request_service": SERVICE, "request_id": preview_token,
			"status": "Queued", "request_description": "Flow 整体审核后创建顺丰替换面单",
			"reference_doctype": "Shipment", "reference_docname": doc.name,
			"data": _json({"version": 1, "user": _actor(), "scope": _scope(), "site": frappe.local.site,
				"shipment": doc.name, "old_waybill": record.get("waybill"), "form": form})})
		ledger.flags._name = _ledger_name(preview_token)
		try:
			ledger.insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			return _existing_execution(preview_token) or {"status": "processing", "verified": False,
				"preview_token": preview_token, "message": "本批准方案已被另一个请求锁定，没有再次请求顺丰。"}
		result = waybill.create_replacement(doc.name, _json(form), plan["reason"], int(shipped))
		result = {"verified": bool(result.get("ok")), "shipment": doc.name,
			"old_waybill": record.get("waybill"), "preview_token": preview_token, **result}
		result["message"] = ("新替换面单已创建，旧单号及历史已保留；未发货场景已按规则自动启用新单。" if result.get("ok") and not shipped else
			"新替换面单已创建，旧单号及历史已保留；已发货场景还需记录顺丰客服反馈并批准启用。" if result.get("ok") else
			result.get("message", "替换面单创建失败。"))
		ledger.status = "Completed"
		ledger.output = _json(result)
		ledger.save(ignore_permissions=True)
		return result
	except Exception as exc:
		if ledger is not None:
			try:
				ledger.status = "Failed"
				ledger.output = _json({"status": "error", "verified": False, "reason": str(exc),
					"message": "本批准方案执行失败；没有再次请求顺丰。"})
				ledger.save(ignore_permissions=True)
			except Exception:
				frappe.log_error(title="SF replacement Flow execution ledger failure")
		return {"status": "error", "verified": False, "reason": str(exc), "message": "替换面单未完成，请按原因处理；原单号和历史不变。"}


@tool(requires_confirmation=True)
def record_sf_waybill_replacement_feedback(shipment: str, waybill_record: str, status: str, note: str):
	"""批准记录顺丰外部客服反馈，不直接切换当前面单。"""
	try:
		if not str(note or "").strip():
			frappe.throw("请填写顺丰外部客服反馈内容。")
		result = waybill.record_replacement_feedback(shipment, waybill_record, status, note)
		return {"verified": True, **result, "message": "已保存顺丰客服反馈；只有状态为待启用时才能继续启用新面单。"}
	except Exception as exc:
		return {"status": "error", "verified": False, "reason": str(exc), "message": "客服反馈未保存。"}


@tool(requires_confirmation=True)
def activate_sf_waybill_replacement(shipment: str, waybill_record: str, reason: str):
	"""批准启用已获顺丰外部确认的替换面单，并保留旧单历史。"""
	try:
		result = waybill.activate_replacement(shipment, waybill_record, reason)
		return {"verified": bool(result.get("ok")), **result,
			"message": "新面单已启用，旧单号与运费历史仍保留。" if result.get("ok") else "替换面单未启用。"}
	except Exception as exc:
		return {"status": "error", "verified": False, "reason": str(exc), "message": "新面单未启用，请先核对外部客服反馈和历史记录。"}


TOOLS = (
	("preview_sf_waybill_replacement", "预检顺丰替换面单", False, "核对未发货或已发货换单场景、旧单号、原因和新参数；不联系顺丰、不创建记录。"),
	("create_sf_waybill_replacement", "批准并创建替换面单", True, "整体批准后创建新顺丰面单；未发货自动启用，已发货保留待替换并等待顺丰外部确认。"),
	("record_sf_waybill_replacement_feedback", "记录顺丰客服换单反馈", True, "批准记录外部顺丰客服反馈；不直接切换当前面单。"),
	("activate_sf_waybill_replacement", "批准启用替换面单", True, "仅已获顺丰客服确认的替换面单可启用，旧单号和历史保留。"),
)
