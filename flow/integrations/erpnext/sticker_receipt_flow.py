"""Reviewed zero-valuation receipts for customer stickers that physically arrived.

This is deliberately separate from Purchase Receipt: the sticker service cost is
already handled by the sales process, so the arrival only adds stock quantity.
The document written is ERPNext's native Stock Entry (Material Receipt).
"""
from __future__ import annotations

from datetime import date
import math

import frappe
from frappe.utils import flt, nowdate
from flow.lib.tool import tool

from .reviewed_flow import InputError, confirmation, execute, preview
from .sales_order_flow import _without_price_maintenance

KIND = "sticker_receipt"
DEFAULT_WAREHOUSE = "大坪仓库 - LEYA"
REQUEST_FIELDS = {
    "sales_order", "customer", "company", "posting_date", "warehouse", "items",
    "submit", "actual_receipt", "new_request", "zero_valuation_reason",
}


def _text(value, field, *, optional=False):
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InputError(f"{field}须提供精确编号或文字，不能使用空值或首尾空格。", [field])
    return value


def _number(value, field):
    if isinstance(value, bool) or value in (None, ""):
        raise InputError(f"{field}必须是明确的正数。", [field])
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise InputError(f"{field}必须是明确的数字。", [field]) from None
    if not math.isfinite(result) or result <= 0:
        raise InputError(f"{field}必须是有限正数。", [field])
    return result


def _read(doctype, name, *, for_update=False):
    doc = frappe.get_doc(doctype, name, for_update=for_update)
    doc.check_permission("read")
    return doc


def _company(name):
    name = name or frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )
    if not name:
        choices = frappe.get_list("Company", pluck="name", limit_page_length=2)
        name = choices[0] if len(choices) == 1 else None
    if not name:
        raise InputError("没有唯一的默认公司，请明确提供本次入库公司。", ["company"])
    return _read("Company", name)


def _warehouse(name, company):
    warehouse = _read("Warehouse", name or DEFAULT_WAREHOUSE)
    if warehouse.company != company or warehouse.get("disabled") or warehouse.get("is_group"):
        raise InputError("收货仓库须是本公司的启用实际仓库。", ["warehouse", "company"])
    return warehouse.name


def _sticker_item(code, customer):
    from erpnext.stock.doctype.item.sticker_stock import is_customer_sticker, sticker_customer

    item = _read("Item", code)
    if item.get("disabled") or not item.get("is_stock_item"):
        raise InputError(f"贴纸物料 {code} 必须启用并维护为库存物料。", ["items"])
    if not is_customer_sticker(item):
        raise InputError(f"物料 {code} 不是已登记的客户贴纸，不能走贴纸到货流程。", ["items"])
    owner = sticker_customer(item)
    if owner != customer:
        raise InputError(f"贴纸 {code} 不属于客户 {customer}，请核对精确物料编号。", ["customer", "items"])
    return item


def _order(sales_order, customer, *, for_update=False):
    order = _read("Sales Order", sales_order, for_update=for_update)
    if order.docstatus != 1 or order.status in ("Cancelled", "Closed"):
        raise InputError("销售订单须已提交且未取消或关闭。", ["sales_order"])
    if order.customer != customer:
        raise InputError("销售订单与指定客户不一致。", ["sales_order", "customer"])
    # ERPNext stores advance_paid in company currency; compare it with the
    # corresponding base total instead of the order-currency grand_total.
    if flt(order.base_grand_total) > flt(order.advance_paid) + 0.000001:
        raise InputError("销售订单尚未完成收款，贴纸到货入库前请先核对订单收款。", ["sales_order"])
    return order


def _sticker_sources(order):
    result = {}
    for packed in order.get("packed_items") or []:
        code = packed.get("item_code")
        if not code:
            continue
        try:
            item = _sticker_item(code, order.customer)
        except InputError as exc:
            # A non-sticker packed component is normal; a name that looks like a
            # sticker but is invalid must still be surfaced by the explicit row.
            if "不是已登记的客户贴纸" in str(exc):
                continue
            raise
        key = (item.name, packed.get("warehouse") or DEFAULT_WAREHOUSE)
        row = result.setdefault(key, {"item_code": item.name, "item_name": item.item_name,
            "stock_uom": item.stock_uom, "warehouse": key[1], "ordered_qty": 0.0})
        row["ordered_qty"] += flt(packed.get("qty"))
    return list(result.values())


def _request(request):
    if not isinstance(request, dict):
        raise InputError("请提供贴纸到货 request 对象。", ["request"])
    result = dict(request)
    unknown = set(result) - REQUEST_FIELDS
    if unknown:
        raise InputError("不支持的入库字段：" + "、".join(sorted(unknown)), sorted(unknown))
    for field in ("sales_order", "customer", "zero_valuation_reason"):
        _text(result.get(field), field)
    for field in ("submit", "actual_receipt", "new_request"):
        if field in result and type(result[field]) is not bool:
            raise InputError(field + "必须为 true 或 false。", [field])
    result.setdefault("submit", False)
    if result["submit"] and result.get("actual_receipt") is not True:
        raise InputError("提交入库必须明确确认本批已实际到货，请提供 actual_receipt=true。", ["actual_receipt"])
    rows = result.get("items")
    if not isinstance(rows, list) or not rows or len(rows) > 100:
        raise InputError("items须逐行提供1至100条贴纸到货明细。", ["items"])
    seen = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise InputError(f"items[{index}]必须是明细对象。", [f"items[{index}]"])
        allowed = {"item_code", "qty", "warehouse"}
        extras = set(row) - allowed
        if extras:
            raise InputError(f"items[{index}]含不支持字段：" + "、".join(sorted(extras)),
                             [f"items[{index}].{key}" for key in sorted(extras)])
        code = _text(row.get("item_code"), f"items[{index}].item_code")
        if code in seen:
            raise InputError("同一贴纸物料不能重复，数量请合并。", ["items"])
        seen.add(code)
        _number(row.get("qty"), f"items[{index}].qty")
        if row.get("warehouse") is not None:
            _text(row["warehouse"], f"items[{index}].warehouse")
    result["posting_date"] = result.get("posting_date") or nowdate()
    try:
        posting_date = date.fromisoformat(_text(result["posting_date"], "posting_date"))
        today = date.fromisoformat(nowdate())
    except (ValueError, InputError):
        raise InputError("入库日期须为 YYYY-MM-DD。", ["posting_date"]) from None
    if str(posting_date) != result["posting_date"] or posting_date > today:
        raise InputError("入库日期不能是未来日期。", ["posting_date"])
    if result.get("company"):
        _text(result["company"], "company")
    if result.get("warehouse"):
        _text(result["warehouse"], "warehouse")
    return result


def _build(request, for_update=False):
    request = _request(request)
    order = _order(request["sales_order"], request["customer"], for_update=for_update)
    company = _company(request.get("company") or order.company)
    sources = {row["item_code"]: row for row in _sticker_sources(order)}
    if not sources:
        raise InputError("该销售订单没有已登记的客户贴纸组件，不能创建贴纸到货入库。", ["sales_order"])
    doc = frappe.new_doc("Stock Entry")
    doc.purpose = "Material Receipt"
    doc.set_stock_entry_type()
    doc.company = company.name
    doc.posting_date = request["posting_date"]
    doc.remarks = (f"FLOW-STICKER-RECEIPT|sales_order={order.name}|customer={order.customer}\n"
                   "零成本入库原因（用户确认）：" + request["zero_valuation_reason"])
    for choice in request["items"]:
        source = sources.get(choice["item_code"])
        if not source:
            raise InputError(f"贴纸 {choice['item_code']} 不在销售订单的贴纸组件中。", ["items"])
        item = _sticker_item(choice["item_code"], order.customer)
        warehouse = _warehouse(choice.get("warehouse") or request.get("warehouse") or source["warehouse"], company.name)
        doc.append("items", {"item_code": item.name, "item_name": item.item_name,
            "qty": _number(choice["qty"], "贴纸到货数量"), "uom": item.stock_uom,
            "stock_uom": item.stock_uom, "conversion_factor": 1, "t_warehouse": warehouse,
            "basic_rate": 0, "allow_zero_valuation_rate": 1, "set_basic_rate_manually": 1})
    doc.check_permission("create")
    if request["submit"]:
        doc.check_permission("submit")
    doc.flags.ignore_permissions = False
    doc.run_method("before_validate")
    doc.run_method("validate")
    for row in doc.items:
        if flt(row.basic_rate) != 0 or flt(row.amount) != 0 or flt(row.valuation_rate) != 0:
            raise InputError("原生库存校验改变了零估值；本次入库已停止，未保存任何单据。", ["items"])
    return doc


def _summary(doc, request):
    items = [{"物料编号": row.item_code, "物料名称": row.item_name, "数量": flt(row.qty),
              "库存单位": row.uom, "入库仓库": row.t_warehouse, "库存估值": 0} for row in doc.items]
    return {"title": "客户贴纸实际到货入库", "action": "提交原生库存入库" if request.get("submit") else "保存库存入库草稿",
            "effects": (["贴纸数量进入库存，库存价值为0；不会创建供应商应付、采购付款或重复定制服务成本。"]
                        if request.get("submit") else ["仅保存草稿，尚未增加库存或记账。"]),
            "fields": {"销售订单": request["sales_order"], "客户": request["customer"], "公司": doc.company,
                       "实际到货日期": str(doc.posting_date), "零成本原因（用户确认）": request["zero_valuation_reason"]},
            "items": items, "totals": {"贴纸总数量": sum(flt(row.qty) for row in doc.items), "库存价值": 0},
            "warnings": ["只有实际到货后才能提交；提交后才能进入出库流程。"]}


def get_sticker_receipt_options(sales_order: str = "", customer: str = ""):
    """读取销售订单中的客户贴纸组件，返回到货入库可选明细；不写入数据。"""
    try:
        sales_order = _text(sales_order, "sales_order")
        customer = _text(customer, "customer")
        order = _order(sales_order, customer)
        sources = _sticker_sources(order)
        if not sources:
            raise InputError("该订单没有客户贴纸组件。", ["sales_order"])
        return {"status": "needs_selection", "reason": "请选择实际已到货的贴纸数量和仓库，然后预检并整体审核。",
                "sales_order": order.name, "customer": order.customer, "company": order.company,
                "items": sources, "default_warehouse": DEFAULT_WAREHOUSE,
                "next": "preview_sticker_receipt→save_sticker_receipt"}
    except Exception as exc:
        return {"status": "needs_input" if isinstance(exc, InputError) else "error", "verified": False,
                "reason": str(exc), "missing": getattr(exc, "fields", [])}


def preview_sticker_receipt(request: dict):
    """预检客户贴纸实际到货；提交时必须同时明确 actual_receipt=true。"""
    return preview(KIND, request, _build, _summary)


def _verify(doc, request):
    if not request.get("submit"):
        return
    for row in doc.items:
        entries = frappe.get_all("Stock Ledger Entry", filters={"voucher_type": "Stock Entry", "voucher_no": doc.name,
            "voucher_detail_no": row.name, "is_cancelled": 0}, fields=["item_code", "warehouse", "actual_qty", "stock_value_difference"])
        if (not entries or any(e.item_code != row.item_code or e.warehouse != row.t_warehouse or flt(e.actual_qty) <= 0 for e in entries)
                or abs(sum(flt(e.actual_qty) for e in entries) - flt(row.transfer_qty)) > 0.000001
                or any(abs(flt(e.stock_value_difference)) > 0.000001 for e in entries)):
            raise InputError("提交后的库存流水与贴纸数量、仓库或零估值不一致，本次入库已撤回。")
    gl = frappe.get_all("GL Entry", filters={"voucher_type": "Stock Entry", "voucher_no": doc.name, "is_cancelled": 0},
                        fields=["debit", "credit"])
    if any(flt(row.debit) != 0 or flt(row.credit) != 0 for row in gl):
        raise InputError("零估值贴纸入库产生了非零会计流水，本次入库已撤回，请先核对公司库存会计配置。")


@tool(requires_confirmation=True, confirm_prompt=lambda args: confirmation(KIND, args.get("preview_token")))
def save_sticker_receipt(preview_token: str):
    """按预检审核卡保存或提交客户贴纸到货库存单；重复执行返回原单。"""
    with _without_price_maintenance():
        return execute(KIND, preview_token, _build, _summary, verify=_verify)


TOOLS = [
    ("get_sticker_receipt_options", "查询客户贴纸到货入库明细", False, "只读列出销售订单中已登记的客户贴纸组件。"),
    ("preview_sticker_receipt", "预检客户贴纸到货入库", False, "生成一次整体审核卡；数量进入库存前不写入。"),
    ("save_sticker_receipt", "保存或提交客户贴纸到货入库", True, "使用审核卡保存草稿或提交原生Material Receipt。"),
]
