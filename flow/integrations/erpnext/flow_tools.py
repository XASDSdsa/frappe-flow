# Flow AI tools for SF International. Imported by Flow Tool rows.
from __future__ import annotations

from typing import Annotated

import frappe
from frappe import _
from frappe.utils import cint, flt

from flow.lib.tool import tool


def _shipping():
	from erpnext_shipping.sf_international import shipping

	return shipping


def _shipments_for_sales_order(sales_order: str) -> list[str]:
	shipping = _shipping()
	dn_items = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": sales_order, "docstatus": ["<", 2]},
		fields=["parent"],
	)
	dn_names = list({row.parent for row in dn_items if row.parent})
	if not dn_names:
		return []
	links = frappe.get_all(
		"Shipment Delivery Note",
		filters={"delivery_note": ["in", dn_names]},
		fields=["parent"],
	)
	names = []
	for row in links:
		name = row.parent
		if not name or name in names or not frappe.db.exists("Shipment", name):
			continue
		status = frappe.db.get_value("Shipment", name, "status") or ""
		if status in shipping.CANCELLED_STATUSES:
			continue
		names.append(name)
	return names


def _shipments_for_delivery_note(delivery_note: str) -> list[str]:
	shipping = _shipping()
	links = frappe.get_all(
		"Shipment Delivery Note",
		filters={"delivery_note": delivery_note},
		fields=["parent"],
	)
	names = []
	for row in links:
		name = row.parent
		if not name or name in names or not frappe.db.exists("Shipment", name):
			continue
		status = frappe.db.get_value("Shipment", name, "status") or ""
		if status in shipping.CANCELLED_STATUSES:
			continue
		names.append(name)
	return names


def _resolve_shipment_names(
	shipment: str | None = None,
	waybill: str | None = None,
	sales_order: str | None = None,
	delivery_note: str | None = None,
) -> list[str]:
	shipment = (shipment or "").strip()
	waybill = (waybill or "").strip()
	sales_order = (sales_order or "").strip()
	delivery_note = (delivery_note or "").strip()
	if shipment:
		if waybill:
			doc = frappe.get_doc("Shipment", shipment)
			doc.check_permission("read")
			if _shipping()._sf_waybill(doc) != waybill:
				frappe.throw("所选单号不是该运单的当前面单，不能将此操作转移到当前面单。历史查询请使用状态、轨迹或运费查询工具。")
		if not frappe.db.exists("Shipment", shipment):
			frappe.throw(_("Shipment {0} was not found.").format(shipment))
		return [shipment]
	if waybill:
		rows = frappe.get_all("Shipment", or_filters={"shipment_id": waybill, "awb_number": waybill}, fields=["name"], limit_page_length=2)
		if not rows:
			frappe.throw(_("No shipment found for waybill {0}.").format(waybill))
		if len(rows) != 1:
			frappe.throw("该单号对应多张运单，不能猜测操作对象。")
		doc = frappe.get_doc("Shipment", rows[0].name)
		doc.check_permission("read")
		if _shipping()._sf_waybill(doc) != waybill:
			frappe.throw("所选单号不是该运单的当前面单，不能将此操作转移到当前面单。")
		return [doc.name]
	if delivery_note:
		names = _shipments_for_delivery_note(delivery_note)
		if not names:
			frappe.throw(_("No SF shipment found for Delivery Note {0}.").format(delivery_note))
		return names
	if sales_order:
		names = _shipments_for_sales_order(sales_order)
		if not names:
			frappe.throw(
				_("销售订单 {0} 尚无顺丰运单；状态查询工具不能用于准备出库，请先调用 get_delivery_note_options(sales_order=订单号)。").format(sales_order)
			)
		return names
	frappe.throw(_("Please pass shipment, waybill, sales_order, or delivery_note."))


def _snapshot(name: str) -> dict:
	shipping = _shipping()
	doc = frappe.get_doc("Shipment", name)
	doc.check_permission("read")
	waybill = shipping._sf_waybill(doc)
	amount = flt(doc.shipment_amount)
	label = doc.get("sf_label_url") if shipping._has_field("Shipment", "sf_label_url") else None
	return {
		"shipment": doc.name,
		"status": doc.status,
		"docstatus": cint(doc.docstatus),
		"service_provider": doc.service_provider,
		"waybill": waybill,
		"freight_status": doc.get("sf_freight_status"),
		"freight_amount": amount or None,
		"freight_journal": doc.get("sf_freight_journal"),
		"tracking_status": doc.tracking_status,
		"tracking_info": doc.tracking_status_info,
		"label_url": label,
		"pickup_address": doc.pickup_address_name,
		"delivery_address": doc.delivery_address_name,
		"delivery_notes": [
			row.delivery_note for row in (doc.shipment_delivery_note or []) if row.delivery_note
		],
	}


def _waybill_api():
	from erpnext_shipping.sf_international import waybill

	return waybill


def _query_targets(shipment=None, waybill=None, sales_order=None, delivery_note=None):
	"""Resolve exact carrier identities for queries, never for lifecycle mutations."""
	shipment = (shipment or "").strip()
	waybill = (waybill or "").strip()
	if sales_order:
		frappe.get_doc("Sales Order", sales_order).check_permission("read")
	if delivery_note:
		frappe.get_doc("Delivery Note", delivery_note).check_permission("read")
	exact = None
	if waybill:
		rows = frappe.get_all("SF Waybill", filters={"waybill": waybill}, fields=["name", "shipment"], limit_page_length=2)
		if len(rows) > 1:
			frappe.throw("该单号对应多条面单记录，请先核对，不能猜测查询对象。")
		if rows:
			exact = frappe.get_doc("SF Waybill", rows[0].name)
			names = [exact.shipment]
		else:
			rows = frappe.get_all("Shipment", or_filters={"shipment_id": waybill, "awb_number": waybill}, fields=["name"], limit_page_length=2)
			if len(rows) != 1:
				frappe.throw("没有找到唯一且可核对的物流面单，请确认单号。")
			names = [rows[0].name]
		if shipment and names != [shipment]:
			frappe.throw("指定单号不属于这张运单，请核对后再查询。")
	else:
		names = _resolve_shipment_names(shipment, None, sales_order, delivery_note)
	out = []
	for name in names:
		doc = frappe.get_doc("Shipment", name)
		doc.check_permission("read")
		if sales_order and name not in _shipments_for_sales_order(sales_order):
			frappe.throw("所选面单与销售订单不匹配。")
		if delivery_note and not any(r.delivery_note == delivery_note for r in doc.get("shipment_delivery_note") or []):
			frappe.throw("所选面单与出库单不匹配。")
		current = _shipping()._sf_waybill(doc)
		if waybill and exact is None and current != waybill:
			frappe.throw("该旧单号没有独立历史记录，不能用当前面单代替查询，请先核对历史记录。")
		record = exact
		if record is None and current:
			matches = frappe.get_all("SF Waybill", filters={"shipment": name, "waybill": current}, fields=["name"], limit_page_length=2)
			if len(matches) > 1:
				frappe.throw("当前单号存在重复历史记录，请先核对。")
			if matches:
				record = frappe.get_doc("SF Waybill", matches[0].name)
		if record:
			if record.get("waybill") == current and not record.has_permission("read"):
				# Keep native current-Shipment access; never expose unreadable history.
				record = None
			else:
				record.check_permission("read")
		is_current = bool(current and (not waybill or current == waybill) and (not record or cint(record.get("is_active"))))
		out.append({"shipment": name, "waybill": waybill or current or None,
			"waybill_record": record.name if record else None, "is_current": is_current,
			"label_scope": ("当前面单" if is_current else "历史面单") if (waybill or current) else "尚未取得面单", "record": record})
	return out


def _query_identity(target):
	return {key: target[key] for key in ("shipment", "waybill", "waybill_record", "is_current", "label_scope")}


def _query_snapshot(target):
	identity = _query_identity(target)
	record = target["record"]
	if not record:
		return {**_snapshot(target["shipment"]), **identity}
	# Only this exact history row supplies amount, status and label URL.
	public = _waybill_api()._public_record(record.as_dict())
	parent = frappe.get_doc("Shipment", target["shipment"])
	return {**identity, "status": parent.status, "docstatus": cint(parent.docstatus),
		"label_status": record.get("replacement_status"),
		"freight_status": record.get("freight_status"), "freight_amount": record.get("freight_amount"),
		"freight_currency": record.get("freight_currency"), "freight_journal": record.get("freight_journal"),
		"freight_accounting_status": record.get("freight_accounting_status"),
		"tracking_status": record.get("tracking_status"), "tracking_info": record.get("tracking_status_info"),
		"label_url": record.get("label_url"), "tracking_events": public.get("tracking_events") or [],
		"carrier_cancelled": cint(record.get("carrier_cancelled"))}


@tool
def get_sf_shipment_status(
	shipment: Annotated[str | None, "系统运单编号"] = None,
	waybill: Annotated[str | None, "明确的当前或历史顺丰单号；指定后只查询该单号"] = None,
	sales_order: Annotated[str | None, "销售订单号，仅定位已有运单"] = None,
	delivery_note: Annotated[str | None, "出库单编号"] = None,
) -> dict:
	"""只读查看当前或历史顺丰面单，以及系统已保存的轨迹、运费和 PDF。

	### 参数与默认值

	- `shipment`、`waybill`、`delivery_note`、`sales_order` 任选其一定位；用户给了具体顺丰单号就传 `waybill`。

	### 返回与下一步

	- 指定 `waybill` 时只返回该单号，并标注当前面单或历史面单。
	- 只给运单、出库单或订单时同时列出可读的面单历史；有多张历史面单须让客服选择，不默认最新一条。
	- 需要承运商最新轨迹用 `query_sf_tracking`，需要运费账单用 `query_sf_freight`。

	### 限制

	- 不请求顺丰、不写入系统。订单尚无运单时改用 `get_delivery_note_options` 准备出库。
	"""
	rows = []
	for target in _query_targets(shipment, waybill, sales_order, delivery_note):
		row = _query_snapshot(target)
		if not waybill:
			history = _waybill_api().list_waybill_records(target["shipment"])
			visible = []
			for r in history.get("waybills") or []:
				if r.get("name"):
					frappe.get_doc("SF Waybill", r["name"]).check_permission("read")
				visible.append({**r, "label_scope": "当前面单" if cint(r.get("is_active")) else "历史面单"})
			row["waybills"] = visible
			row["history_available"] = history.get("history_available", True)
		rows.append(row)
	return {"count": len(rows), "shipments": rows}


@tool(requires_confirmation=True)
def query_sf_freight(
	shipment: Annotated[str | None, "系统运单编号"] = None,
	waybill: Annotated[str | None, "要查询的明确当前或历史顺丰单号"] = None,
	sales_order: Annotated[str | None, "销售订单编号，仅定位已有运单"] = None,
	delivery_note: Annotated[str | None, "出库单编号，仅定位已有运单"] = None,
) -> dict:
	"""批准后向顺丰同步明确面单的运费账单，并按现有结算规则保存。

	### 参数与默认值

	- `shipment`、`waybill`、`delivery_note`、`sales_order` 任选其一定位；查询历史面单必须传该 `waybill`。

	### 返回与下一步

	- 每张面单单独返回运费结果，并标注当前面单或历史面单。
	- 当前面单沿用现有结算记账规则；历史账单单独保存并交财务核对。

	### 限制

	- 会写入运费记录，保留一次原生批准。指定旧号不会改查当前号，不覆盖当前面单费用；没有结算账单不代表历史费用可删除。
	"""
	out = []
	for target in _query_targets(shipment, waybill, sales_order, delivery_note):
		if target["waybill_record"]:
			result = _waybill_api().fetch_waybill_freight(target["shipment"], target["waybill_record"])
		else:
			result = _shipping().fetch_sf_freight(target["shipment"])
		out.append({**(result or {}), **_query_identity(target)})
	return {"results": out}


@tool
def query_sf_tracking(
	shipment: Annotated[str | None, "系统运单编号"] = None,
	waybill: Annotated[str | None, "要查询的明确当前或历史顺丰单号"] = None,
	sales_order: Annotated[str | None, "销售订单编号，仅定位已有运单"] = None,
	delivery_note: Annotated[str | None, "出库单编号，仅定位已有运单"] = None,
) -> dict:
	"""只读向顺丰查询明确面单的完整物流轨迹。

	### 参数与默认值

	- `shipment`、`waybill`、`delivery_note`、`sales_order` 任选其一定位；查询历史面单必须传该 `waybill`。

	### 返回与下一步

	- 返回顺丰轨迹和已保存历史，并标注当前面单或历史面单。
	- 用户要求把最新轨迹保存回系统时，再调用 `sync_sf_tracking`。

	### 限制

	- 不改写运单或历史面单，无需批准。
	"""
	out = []
	for target in _query_targets(shipment, waybill, sales_order, delivery_note):
		if target["waybill_record"]:
			tracking = _waybill_api().fetch_waybill_tracking_readonly(target["shipment"], target["waybill_record"])
		else:
			tracking = _shipping().track_sf_shipment_readonly(target["shipment"], target["waybill"])
		out.append({**_query_identity(target), "tracking": tracking or _query_snapshot(target)})
	return {"results": out}


@tool(requires_confirmation=True)
def sync_sf_tracking(
	shipment: Annotated[str | None, "系统运单编号"] = None,
	waybill: Annotated[str | None, "要同步的明确当前或历史顺丰单号"] = None,
	sales_order: Annotated[str | None, "销售订单编号，仅定位已有运单"] = None,
	delivery_note: Annotated[str | None, "出库单编号，仅定位已有运单"] = None,
) -> dict:
	"""批准后向顺丰查询并保存明确面单的最新物流轨迹。

	### 参数与默认值

	- `shipment`、`waybill`、`delivery_note`、`sales_order` 任选其一定位；同步历史面单必须传该 `waybill`。

	### 返回与下一步

	- 返回已保存的最新轨迹，`persisted=true` 表示已写回对应运单或历史面单。

	### 限制

	- 会写入系统，保留一次原生批准；只看轨迹时用只读的 `query_sf_tracking`。
	"""
	out = []
	for target in _query_targets(shipment, waybill, sales_order, delivery_note):
		if target["waybill_record"]:
			tracking = _waybill_api().fetch_waybill_tracking(target["shipment"], target["waybill_record"])
		else:
			doc = frappe.get_doc("Shipment", target["shipment"])
			doc.check_permission("write")
			tracking = _shipping().update_tracking(doc.name, doc.service_provider, target["waybill"],
				[r.delivery_note for r in doc.get("shipment_delivery_note") or [] if r.delivery_note])
		out.append({**_query_identity(target), "tracking": tracking or _query_snapshot(target), "persisted": True})
	return {"results": out}


@tool(requires_confirmation=True)
def book_sf_waybill(shipment: Annotated[str, "旧入口，已停用"]):
	"""Retired compatibility symbol: old queued approvals must never place orders."""
	return {"ok": False, "status": "retired", "verified": False, "shipment": shipment,
		"message": "旧顺丰下单工具已停用，本次未下单。请使用prepare_sf_label、preview_sf_label、create_sf_label完成地址选择和整体审核。"}


@tool(requires_confirmation=True)
def print_sf_label(
	shipment: Annotated[str | None, "Shipment name"] = None,
	waybill: Annotated[str | None, "SF waybill number"] = None,
) -> dict:
	"""Print/generate the SF International shipping label PDF and attach it to the Shipment."""
	shipping = _shipping()
	name = _resolve_shipment_names(shipment, waybill)[0]
	url = shipping.print_shipping_label(name)
	return {"ok": True, "shipment": name, "label_url": url}


@tool(requires_confirmation=True)
def dispatch_sf_shipment(
	shipment: Annotated[str | None, "Shipment name"] = None,
	waybill: Annotated[str | None, "SF waybill number"] = None,
) -> dict:
	"""Print the SF label and mark the shipment as 已发货. The shipment must already be submitted."""
	shipping = _shipping()
	name = _resolve_shipment_names(shipment, waybill)[0]
	url = shipping.dispatch_sf_shipment(name)
	return {"ok": True, "shipment": name, "status": "已发货", "label_url": url}


@tool(requires_confirmation=True)
def cancel_sf_waybill(
	shipment: Annotated[str | None, "Shipment name"] = None,
	waybill: Annotated[str | None, "SF waybill number"] = None,
) -> dict:
	"""Cancel the SF International label only. The ERP Shipment is kept so a new label can be created."""
	shipping = _shipping()
	name = _resolve_shipment_names(shipment, waybill)[0]
	result = shipping.cancel_sf_shipment(name) or {}
	result["shipment"] = name
	return result


@tool(requires_confirmation=True)
def recreate_sf_waybill(
	shipment: Annotated[str | None, "Shipment name"] = None,
	waybill: Annotated[str | None, "Cancelled SF waybill / shipment lookup"] = None,
) -> dict:
	"""Create a new SF International waybill after the previous label was cancelled. Uses saved sender/receiver."""
	shipping = _shipping()
	name = _resolve_shipment_names(shipment, waybill)[0]
	result = shipping.recreate_sf_shipment(name) or {}
	result["shipment"] = name
	return result


@tool
def lookup_sf_postcode(
	country_code: Annotated[str, "两位国家代码，例如 US 或 CN"],
	post_code: Annotated[str, "要反查的邮编"],
) -> dict:
	"""按国家和邮编向顺丰反查可用的省州、城市、区县。

	### 参数与默认值

	- `country_code` 为两位国家代码；`post_code` 为客户档案或客服给出的邮编。

	### 返回与下一步

	- 返回顺丰实际候选；创建面单时改用 `prepare_sf_label`，由它自动反查并让客服选择地址。

	### 限制

	- 只读，不保存地址、不创建面单；不能编造或补全接口未返回的地区。
	"""
	shipping = _shipping()
	rows = shipping.lookup_sf_postcode(country_code, post_code) or []
	return {"count": len(rows), "matches": rows}


TOOL_TITLES = {
	"get_sf_shipment_status": "查询顺丰运单",
	"query_sf_freight": "同步顺丰运费账单",
	"query_sf_tracking": "查询顺丰轨迹",
	"sync_sf_tracking": "同步并保存顺丰轨迹",
	"print_sf_label": "打印顺丰面单",
	"dispatch_sf_shipment": "打印面单并发货",
	"cancel_sf_waybill": "取消顺丰面单",
	"recreate_sf_waybill": "重新创建顺丰面单",
	"lookup_sf_postcode": "顺丰邮编反查",
}

FLOW_TOOL_OBJECTS = [
	get_sf_shipment_status,
	query_sf_freight,
	query_sf_tracking,
	sync_sf_tracking,
	lookup_sf_postcode,
]
