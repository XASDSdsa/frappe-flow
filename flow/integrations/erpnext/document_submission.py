"""Reviewed native submission for existing Sales Orders and Delivery Notes.

This boundary never creates, edits, or submits a new document during preview. It
only submits an existing draft after one approval, while binding that approval
to the authenticated user, Flow session, document revision, and document name.
"""
from __future__ import annotations

from datetime import timedelta
import html
import json
import re
import uuid
from urllib.parse import quote

import frappe
from frappe.utils import get_datetime, now_datetime, strip_html
from flow.lib.tool import tool

from .sales_order_flow import _actor, _hash

TTL = 1800
KINDS = {"Sales Order": "销售订单", "Delivery Note": "出库单"}
SERVICE = "Flow Reviewed Document Submission"


def _scope():
	"""Bind previews to the active Flow conversation or authenticated web session."""
	run_name = frappe.flags.get("flow_run")
	if run_name:
		run = frappe.db.get_value("Flow Run", run_name, ["owner", "session"], as_dict=True)
		if not run or run.owner != frappe.session.user or not run.session:
			raise frappe.PermissionError("无法核实当前对话的实际操作人，请重新登录后继续。")
		return "flow:" + run.session
	sid = getattr(frappe.session, "sid", None)
	if not sid:
		raise frappe.PermissionError("无法核实当前登录会话，请重新登录后继续。")
	return "session:" + sid


class SubmissionInputError(Exception):
	def __init__(self, message, fields=None):
		super().__init__(message)
		self.fields = fields or []


def _key(token):
	if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
		raise SubmissionInputError("提交审核令牌无效，请重新预检。", ["submission_token"])
	return "flow-document-submission:" + token


def _load(token):
	plan = frappe.cache.get_value(_key(token))
	if not plan or now_datetime() > get_datetime(plan["expires_at"]):
		raise SubmissionInputError("提交审核已过期，请重新预检。", ["submission_token"])
	if (plan.get("user"), plan.get("scope"), plan.get("site")) != (_actor(), _scope(), frappe.local.site):
		raise frappe.PermissionError("此提交审核不属于当前登录账号或当前对话。")
	if plan.get("fingerprint") != _fingerprint(plan):
		raise SubmissionInputError("提交审核摘要校验失败，请重新预检。", ["submission_token"])
	return plan


def _fingerprint(plan):
	return _hash({
		"doctype": plan["doctype"], "document": plan["document"],
		"modified": plan["summary"]["modified"], "user": plan["user"],
		"scope": plan["scope"], "site": plan["site"],
	})


def _read_existing(doctype, name, *, for_update=False):
	if doctype not in KINDS:
		raise SubmissionInputError("只允许提交销售订单或出库单。", ["doctype"])
	if not isinstance(name, str) or not name.strip():
		raise SubmissionInputError("请提供准确的原生单据编号。", ["document"])
	doc = frappe.get_doc(doctype, name.strip(), for_update=for_update)
	doc.check_permission("read")
	if doc.docstatus == 2:
		raise SubmissionInputError("该单据已取消，不能提交；请在原生页面处理。", ["document"])
	if doc.docstatus == 1:
		return doc
	doc.check_permission("submit")
	return doc


def _summary(doc):
	rows = []
	for row in doc.get("items") or []:
		rows.append({
			"row_no": row.idx,
			"item_code": row.get("item_code") or "",
			"item_name": row.get("item_name") or row.get("item_code") or "",
			"qty": float(row.get("qty") or 0),
			"uom": row.get("uom") or "",
			"delivery_date": str(row.get("delivery_date") or ""),
			"warehouse": row.get("warehouse") or "",
			"rate": float(row.get("rate") or 0),
			"amount": float(row.get("amount") or 0),
		})
	return {
		"title": "提交" + KINDS[doc.doctype],
		"action": "提交" + KINDS[doc.doctype],
		"doctype": doc.doctype,
		"document": doc.name,
		"customer": doc.get("customer") or "",
		"customer_name": doc.get("customer_name") or doc.get("customer") or "",
		"company": doc.get("company") or "",
		"currency": doc.get("currency") or "",
		"address": strip_html(str(doc.get("address_display") or "")).strip(),
		"shipping_address": doc.get("shipping_address_name") or "",
		"status": doc.get("status") or "草稿",
		"docstatus": int(doc.docstatus or 0),
		"modified": str(doc.get("modified") or ""),
		"grand_total": float(doc.get("grand_total") or 0),
		"posting_date": str(doc.get("posting_date") or doc.get("transaction_date") or ""),
		"items": rows,
		"effects": [
			"提交后由 ERPNext 原生校验执行；销售订单提交不扣库存，出库单提交按原生库存、会计和仓库规则处理。",
			"本工具不创建新单、不改变商品数量、不调用物流接口。",
		],
	}


def _url(doc):
	return "/desk/" + frappe.scrub(doc.doctype).replace("_", "-") + "/" + quote(doc.name, safe="")


def _failure(exc):
	reason = strip_html(str(exc)).strip() or type(exc).__name__
	return {"status": "needs_input" if isinstance(exc, SubmissionInputError) else "error",
			"verified": False, "reason": reason, "missing": getattr(exc, "fields", []),
			"message": "提交未完成，原单没有被当作已提交；请按原因处理后重新预检。"}


def preview_document_submission(doctype: str, document: str):
	"""只读预检现有销售订单或出库单，返回一次提交审核令牌。"""
	try:
		_actor()
		doc = _read_existing(doctype, document)
		if doc.docstatus == 1:
			return {"status": "already_submitted", "verified": True, "doctype": doc.doctype,
					"document": doc.name, "summary": _summary(doc), "url": _url(doc),
					"message": "该单据已经提交，没有重复提交。"}
		summary = _summary(doc)
		plan = {"doctype": doc.doctype, "document": doc.name, "summary": summary,
				"user": frappe.session.user, "scope": _scope(), "site": frappe.local.site,
				"expires_at": str(now_datetime() + timedelta(seconds=TTL))}
		plan["fingerprint"] = _fingerprint(plan)
		token = uuid.uuid4().hex
		frappe.cache.set_value(_key(token), plan, expires_in_sec=TTL)
		return {"status": "preview", "verified": False, "submission_token": token,
				"doctype": doc.doctype, "document": doc.name, "summary": summary,
				"message": "提交审核已准备，尚未提交；请核对审核卡后批准一次。"}
	except Exception as exc:
		return _failure(exc)


def _confirmation_prompt(args):
	try:
		plan = _load((args or {}).get("submission_token"))
		summary = plan["summary"]
		safe = lambda value: html.escape(strip_html(str(value))).replace("\n", " ")
		lines = ["🔵 请整体审核：" + safe(summary["action"]), "",
			"单据：" + safe(summary["document"]), "客户：" + safe(summary["customer_name"]),
			"公司：" + safe(summary["company"]), "当前状态：" + safe(summary["status"]),
			"日期：" + safe(summary["posting_date"])]
		if summary.get("address") or summary.get("shipping_address"):
			lines.append("收货地址：" + safe(summary.get("address") or summary.get("shipping_address")))
		lines.append("")
		lines.append("【商品明细】")
		for row in summary["items"]:
			lines.append(f"• 第{row['row_no']}行 {safe(row['item_name'])}：{row['qty']:g} {safe(row['uom'])}；日期：{safe(row['delivery_date'])}；仓库：{safe(row['warehouse'])}；金额：{safe(summary['currency'])} {row['amount']:g}")
		lines += ["", "总额：" + safe(summary["currency"]) + " " + f"{summary['grand_total']:g}",
			"提交后效果：" + safe(summary["effects"][0]),
			"请核对单据、客户、商品、数量、仓库和金额；正确后批准一次，需修改请选择拒绝。"]
		return "\n".join(lines)
	except Exception as exc:
		return "🔴 提交审核卡生成失败，尚未提交：" + strip_html(str(exc))


def submit_reviewed_document(submission_token: str):
	"""使用真实审核令牌提交一张现有原生草稿；重复调用只回读，不重复提交。"""
	point = "flow_document_submission_" + uuid.uuid4().hex
	savepoint_created = False
	try:
		_actor()
		plan = _load(submission_token)
		doc = _read_existing(plan["doctype"], plan["document"], for_update=True)
		current = _summary(doc)
		if current["modified"] != plan["summary"]["modified"] or _hash(current) != _hash(plan["summary"]):
			return {"status": "needs_review", "verified": False, "doctype": doc.doctype,
					"document": doc.name, "summary": current,
					"message": "审核后原单内容或版本发生变化；未执行本次提交，请重新预检并审核当前单据。"}
		if doc.docstatus == 1:
			return {"status": "already_submitted", "verified": True, "doctype": doc.doctype,
					"document": doc.name, "summary": current, "url": _url(doc),
					"message": "该单据已提交且与审核内容一致，未重复提交。"}
		frappe.db.savepoint(point)
		savepoint_created = True
		doc.submit()
		saved = frappe.get_doc(doc.doctype, doc.name)
		saved.check_permission("read")
		if saved.docstatus != 1:
			raise SubmissionInputError("原生提交调用后回读仍不是已提交状态，当前结果待核对。")
		return {"status": "submitted", "verified": True, "doctype": saved.doctype,
				"document": saved.name, "docstatus": int(saved.docstatus), "summary": _summary(saved),
				"url": _url(saved), "message": "已按审核内容完成原生提交并回读核验。"}
	except Exception as exc:
		if savepoint_created:
			try:
				frappe.db.rollback(save_point=point)
			except Exception as rollback_exc:
				return {"status": "error", "verified": False,
					"reason": strip_html(str(exc)).strip() or type(exc).__name__,
					"rollback_reason": strip_html(str(rollback_exc)).strip() or type(rollback_exc).__name__,
					"message": "原生提交失败，且保存点回滚失败；请立即核对单据状态后再操作。"}
		return _failure(exc)


preview_document_submission = tool(preview_document_submission, name="preview_document_submission")
submit_reviewed_document = tool(submit_reviewed_document, name="submit_reviewed_document",
								requires_confirmation=True, confirm_prompt=_confirmation_prompt)
