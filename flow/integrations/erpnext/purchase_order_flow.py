"""Reviewed native Purchase Order creation for the procurement workflow."""
from datetime import date
import math

import frappe
from frappe.utils import nowdate
from flow.lib.tool import tool

from .reviewed_flow import InputError, confirmation, execute, preview
from .purchase_receipt_flow import DEFAULT_WAREHOUSE
from .sales_order_flow import _without_price_maintenance

KIND = "purchase_order"
FIELDS = {"supplier", "company", "currency", "transaction_date", "schedule_date", "warehouse",
          "items", "submit", "existing_document", "new_request"}


def _text(value, field, optional=False):
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InputError(f"{field}必须是精确编号或非空文字。", [field])
    return value


def _number(value, field):
    if isinstance(value, bool):
        raise InputError(f"{field}必须是有限正数。", [field])
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise InputError(f"{field}必须是有限正数。", [field]) from None
    if not math.isfinite(result) or result <= 0:
        raise InputError(f"{field}必须是有限正数。", [field])
    return result


def _read(doctype, name, lock=False):
    doc = frappe.get_doc(doctype, name, for_update=lock)
    doc.check_permission("read")
    return doc


def _supplier(name):
    supplier = _read("Supplier", name)
    if supplier.get("disabled") or supplier.get("is_internal_supplier"):
        raise InputError("供应商已停用或属于内部调拨供应商。", ["supplier"])
    return supplier


def _company(name):
    name = name or frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
        "Global Defaults", "default_company")
    if not name:
        raise InputError("没有唯一默认公司，请提供 company。", ["company"])
    return _read("Company", name)


def _item(code):
    item = _read("Item", code)
    if item.get("disabled") or item.get("has_variants") or not item.get("is_purchase_item"):
        raise InputError(f"物料 {code} 须是启用且允许采购的明确物料。", ["items"])
    if item.get("has_serial_no") or item.get("has_batch_no"):
        raise InputError(f"物料 {code} 需要序列号或批次，请使用原生采购页面。", ["items"])
    return item


def _request(request):
    if not isinstance(request, dict) or set(request) - FIELDS:
        raise InputError("采购订单参数包含不支持的字段。", ["request"])
    result = dict(request)
    for flag in ("submit", "new_request"):
        if flag in result and type(result[flag]) is not bool:
            raise InputError(flag + " 必须为 true 或 false。", [flag])
    result.setdefault("submit", False)
    if result.get("existing_document"):
        if set(result) - {"existing_document", "submit", "new_request"}:
            raise InputError("已有采购订单草稿只能按原单审核，不能同时覆盖字段。", ["existing_document"])
        _text(result["existing_document"], "existing_document")
        return result
    _text(result.get("supplier"), "supplier")
    rows = result.get("items")
    if not isinstance(rows, list) or not rows or len(rows) > 100:
        raise InputError("请逐行提供采购物料、数量、真实单位成本和仓库。", ["items"])
    seen = set()
    for index, row in enumerate(rows, 1):
        prefix = f"items[{index}]"
        if not isinstance(row, dict) or set(row) - {"item_code", "qty", "rate", "warehouse", "uom", "schedule_date"}:
            raise InputError(f"{prefix} 必须只包含物料、数量、成本、单位、仓库和交期。", [prefix])
        code = _text(row.get("item_code"), prefix + ".item_code")
        if code in seen:
            raise InputError("同一采购物料不可重复，请合并数量。", [prefix + ".item_code"])
        seen.add(code)
        _number(row.get("qty"), prefix + ".qty")
        _number(row.get("rate"), prefix + ".rate")
        if row.get("warehouse") is not None:
            _text(row["warehouse"], prefix + ".warehouse")
        if row.get("uom") is not None:
            _text(row["uom"], prefix + ".uom")
        if row.get("schedule_date"):
            try:
                date.fromisoformat(_text(row["schedule_date"], prefix + ".schedule_date"))
            except ValueError:
                raise InputError(prefix + ".schedule_date 须为 YYYY-MM-DD。", [prefix + ".schedule_date"]) from None
    for key in ("company", "currency", "transaction_date", "schedule_date", "warehouse"):
        if result.get(key):
            _text(result[key], key)
    result.setdefault("transaction_date", nowdate())
    try:
        date.fromisoformat(result["transaction_date"])
    except ValueError:
        raise InputError("transaction_date 须为 YYYY-MM-DD。", ["transaction_date"]) from None
    return result


def _validate(doc, request):
    if doc.docstatus != 0:
        raise InputError("只能处理采购订单草稿；已提交或已取消单据不能重复保存。")
    doc.check_permission("create" if doc.is_new() else "write")
    if request.get("submit"):
        doc.check_permission("submit")
    before = [(r.item_code, float(r.qty), float(r.rate), r.warehouse, r.uom) for r in doc.items]
    doc.run_method("before_validate")
    doc.run_method("validate")
    doc.set_parent_in_children()
    original_name = doc.name
    if not original_name:
        doc.name = "new-flow-purchase-order-preview"
        doc.set_parent_in_children()
    try:
        doc._validate()
    finally:
        doc.name = original_name
        doc.set_parent_in_children()
    after = [(r.item_code, float(r.qty), float(r.rate), r.warehouse, r.uom) for r in doc.items]
    if before != after:
        raise InputError("原生校验改变了已审核的物料、数量、单价、单位或仓库，请重新预检。", ["items"])
    return doc


def _build(request, for_update=False):
    request = _request(request)
    if request.get("existing_document"):
        doc = _read("Purchase Order", request["existing_document"], lock=for_update)
        _validate(doc, request)
        return doc
    supplier = _supplier(request["supplier"])
    company = _company(request.get("company"))
    doc = frappe.new_doc("Purchase Order")
    doc.supplier, doc.company = supplier.name, company.name
    doc.currency = request.get("currency") or supplier.get("default_currency") or company.get("default_currency")
    if not doc.currency:
        raise InputError("缺少实际采购结算币种，请提供 currency。", ["currency"])
    doc.transaction_date = request["transaction_date"]
    doc.schedule_date = request.get("schedule_date") or request["transaction_date"]
    doc.ignore_pricing_rule = 1
    for choice in request["items"]:
        item = _item(choice["item_code"])
        uom = choice.get("uom") or item.get("purchase_uom") or item.stock_uom
        factor = 1.0 if uom == item.stock_uom else next((r.conversion_factor for r in item.get("uoms") or [] if r.uom == uom), None)
        if not factor:
            raise InputError(f"物料 {item.name} 未配置唯一的 {uom} 单位换算。", ["items"])
        warehouse = choice.get("warehouse") or request.get("warehouse") or DEFAULT_WAREHOUSE
        wh = _read("Warehouse", warehouse)
        if wh.company != company.name or wh.get("disabled") or wh.get("is_group"):
            raise InputError(f"仓库 {warehouse} 不是本公司的启用实际仓库。", ["items.warehouse"])
        doc.append("items", {"item_code": item.name, "qty": _number(choice["qty"], "采购数量"),
            "rate": _number(choice["rate"], "真实采购单位成本"), "uom": uom,
            "stock_uom": item.stock_uom, "conversion_factor": factor, "warehouse": warehouse,
            "schedule_date": choice.get("schedule_date") or request.get("schedule_date") or request["transaction_date"]})
    doc.check_permission("create")
    if request.get("submit"):
        doc.check_permission("submit")
    expected = [(r.item_code, float(r.qty), float(r.rate), r.warehouse, r.uom) for r in doc.items]
    doc.run_method("set_missing_values")
    doc.calculate_taxes_and_totals()
    actual = [(r.item_code, float(r.qty), float(r.rate), r.warehouse, r.uom) for r in doc.items]
    if expected != actual:
        raise InputError("原生定价改变了已审核的采购内容，请先核对价格规则。", ["items"])
    return _validate(doc, request)


def _summary(doc, request):
    return {"title": "采购订单", "action": "提交采购订单" if request.get("submit") else "保存采购订单草稿",
        "fields": {"采购订单": doc.name if not doc.is_new() else "待生成", "供应商": doc.supplier_name,
                   "公司": doc.company, "币种": doc.currency, "订单日期": str(doc.transaction_date),
                   "交期": str(doc.schedule_date)},
        "items": [{"物料编号": r.item_code, "物料名称": r.item_name, "数量": float(r.qty),
                   "单位": r.uom, "单位成本": float(r.rate), "仓库": r.warehouse,
                   "交期": str(r.schedule_date)} for r in doc.items],
        "totals": {"未税金额": float(doc.net_total), "税费": float(doc.total_taxes_and_charges or 0),
                   "总额": float(doc.grand_total)},
        "effects": ["提交后形成采购订单和待收货数量；不会因为创建订单直接增加实存或记账。"
                    if request.get("submit") else "保存草稿，尚未提交、收货或增加库存。"]}


def preview_purchase_order(request: dict):
    """预检采购订单；真实供应商、物料、数量、成本和仓库齐全后返回审核令牌。"""
    try:
        return preview(KIND, _request(request), _build, _summary)
    except Exception as exc:
        return {"status": "needs_input" if isinstance(exc, InputError) else "error", "verified": False,
                "reason": str(exc), "missing": getattr(exc, "fields", [])}


@tool(requires_confirmation=True, confirm_prompt=lambda args: confirmation(KIND, args.get("preview_token")))
def save_purchase_order(preview_token: str):
    """整体批准后保存或提交原生采购订单；重复执行返回同一张单。"""
    with _without_price_maintenance():
        return execute(KIND, preview_token, _build, _summary)


TOOLS = (
    ("preview_purchase_order", "预检采购订单", False,
     "核对真实供应商、物料、数量、采购成本、仓库和交期；不保存、不增加库存。"),
    ("save_purchase_order", "保存或提交采购订单", True,
     "整体批准一次后保存草稿或按明确要求提交原生采购订单；不直接收货、不登记付款。"),
)
