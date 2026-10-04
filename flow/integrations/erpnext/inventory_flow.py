"""Read-only inventory, item and supplier selectors for Flow.

All quantities come from ERPNext's native ``Bin`` aggregate.  This module
never writes ``Bin`` or stock ledger rows; stock changes remain native document
operations such as Purchase Receipt and Delivery Note.
"""
from __future__ import annotations

import math

import frappe

from .reviewed_flow import InputError


PAGE_SIZE = 20
MAX_PAGE = 100


def _text(value, field, *, optional=False):
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InputError(f"{field}必须是精确编号或非空文字。", [field])
    return value


def _start(value):
    if type(value) is not int or value < 0 or value > MAX_PAGE * 1000:
        raise InputError("start 必须是合理的非负整数。", ["start"])
    return value


def _number(value, field):
    try:
        result = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        raise InputError(f"{field}不是有效数字。", [field]) from None
    if not math.isfinite(result):
        raise InputError(f"{field}不是有限数字。", [field])
    return result


def _failure(exc):
    return {"status": "needs_input" if isinstance(exc, InputError) else "error",
            "verified": False, "reason": str(exc),
            "missing": getattr(exc, "fields", [])}


def _page(rows, start, *, reason):
    rows = list(rows)
    page = rows[:PAGE_SIZE]
    return {"status": "found" if page else "not_found", "verified": True,
            "items": page, "has_more": len(rows) > PAGE_SIZE,
            "next_start": start + PAGE_SIZE if len(rows) > PAGE_SIZE else None,
            "reason": reason}


def get_inventory_options(item_code: str = "", warehouse: str = "", item_query: str = "",
                          start: int = 0, include_zero: bool = False):
    """### 参数与默认值

    - 使用准确 `item_code` 或 `item_query` 筛选物料，两者互斥；`warehouse` 可选。
    - `start=0`；`include_zero=false`，需要零实存记录时传 `true`。

    ### 返回与下一步

    - 分别展示 `actual_qty` 实存、`reserved_qty` 预留、`available_qty`（实存减预留）和 `projected_qty` 预计量，以及估值和库存单位。
    - 结果分页，按 `has_more` 和 `next_start` 读取后续记录。

    ### 限制

    - 不把预计量当现有库存，不修改 Bin 或库存流水，不要求批准。
    """
    try:
        start = _start(start)
        if type(include_zero) is not bool:
            raise InputError("include_zero 必须为 true 或 false。", ["include_zero"])
        item_code = _text(item_code, "item_code", optional=True)
        warehouse = _text(warehouse, "warehouse", optional=True)
        item_query = _text(item_query, "item_query", optional=True)
        if item_code and item_query:
            raise InputError("item_code 与 item_query 只能选择一个。", ["item_code", "item_query"])
        if item_code:
            item = frappe.get_doc("Item", item_code)
            item.check_permission("read")
        if warehouse:
            wh = frappe.get_doc("Warehouse", warehouse)
            wh.check_permission("read")
        filters = {}
        if item_code:
            filters["item_code"] = item_code
        if warehouse:
            filters["warehouse"] = warehouse
        if not include_zero:
            filters["actual_qty"] = ["!=", 0]
        or_filters = None
        if item_query:
            or_filters = {"item_code": ["like", f"%{item_query}%"],
                          "item_name": ["like", f"%{item_query}%"]}
        rows = frappe.get_list(
            "Bin", filters=filters, or_filters=or_filters,
            fields=["item_code", "warehouse", "actual_qty", "reserved_qty", "ordered_qty",
                    "indented_qty", "planned_qty", "projected_qty", "reserved_stock",
                    "stock_uom", "company", "valuation_rate", "stock_value"],
            order_by="item_code asc, warehouse asc", limit_start=start,
            limit_page_length=PAGE_SIZE + 1,
        )
        result = []
        for row in rows:
            actual = _number(row.actual_qty, "actual_qty")
            reserved = _number(row.reserved_qty, "reserved_qty")
            result.append({"item_code": row.item_code, "warehouse": row.warehouse,
                "actual_qty": actual, "reserved_qty": reserved,
                "available_qty": actual - reserved,
                "ordered_qty": _number(row.ordered_qty, "ordered_qty"),
                "indented_qty": _number(row.indented_qty, "indented_qty"),
                "planned_qty": _number(row.planned_qty, "planned_qty"),
                "projected_qty": _number(row.projected_qty, "projected_qty"),
                "reserved_stock": _number(row.reserved_stock, "reserved_stock"),
                "stock_uom": row.stock_uom, "company": row.company,
                "valuation_rate": _number(row.valuation_rate, "valuation_rate"),
                "stock_value": _number(row.stock_value, "stock_value")})
        return _page(result, start, reason=(
            "actual_qty 是仓库实存，reserved_qty 是已预留；available_qty 仅为 actual_qty-reserved_qty，"
            "projected_qty 单独表示预计库存，不能当作当前实存。"))
    except Exception as exc:
        return _failure(exc)


def get_item_options(item_code: str = "", query: str = "", stock_only: bool = False,
                     purchase_only: bool = False, start: int = 0):
    """### 参数与默认值

    - 精确 `item_code` 优先，也可用 `query` 筛选名称，两者互斥。
    - `stock_only=false`、`purchase_only=false`、`start=0`。

    ### 返回与下一步

    - 返回准确物料编号和属性；按 `has_more`、`next_start` 分页，使用返回编号继续采购或查询库存。

    ### 限制

    - 不自动选择同名物料或第一条候选。
    """
    try:
        start = _start(start)
        if type(stock_only) is not bool or type(purchase_only) is not bool:
            raise InputError("stock_only 与 purchase_only 必须为 true 或 false。", ["stock_only", "purchase_only"])
        item_code = _text(item_code, "item_code", optional=True)
        query = _text(query, "query", optional=True)
        if item_code and query:
            raise InputError("item_code 与 query 只能选择一个。", ["item_code", "query"])
        if item_code:
            item = frappe.get_doc("Item", item_code)
            item.check_permission("read")
        filters = {"disabled": 0}
        if item_code:
            filters["name"] = item_code
        if stock_only:
            filters["is_stock_item"] = 1
        if purchase_only:
            filters["is_purchase_item"] = 1
        or_filters = None
        if query:
            or_filters = {"name": ["like", f"%{query}%"],
                          "item_name": ["like", f"%{query}%"]}
        rows = frappe.get_list("Item", filters=filters, or_filters=or_filters,
            fields=["name as item_code", "item_name", "item_group", "is_stock_item",
                    "is_purchase_item", "stock_uom", "purchase_uom", "has_serial_no",
                    "has_batch_no"], order_by="name asc", limit_start=start,
            limit_page_length=PAGE_SIZE + 1)
        return _page([dict(row) for row in rows], start,
                     reason="只返回启用物料；请使用返回的精确物料编号继续采购或查询库存。")
    except Exception as exc:
        return _failure(exc)


def get_supplier_options(supplier: str = "", query: str = "", start: int = 0):
    """### 参数与默认值

    - 精确 `supplier` 优先，也可用 `query` 查找，两者互斥；`start=0`。

    ### 返回与下一步

    - 支持精确查重；按 `has_more`、`next_start` 分页，使用准确编号继续采购。

    ### 限制

    - 不自动选择同名供应商或第一条候选。
    """
    try:
        start = _start(start)
        supplier = _text(supplier, "supplier", optional=True)
        query = _text(query, "query", optional=True)
        if supplier and query:
            raise InputError("supplier 与 query 只能选择一个。", ["supplier", "query"])
        if supplier:
            doc = frappe.get_doc("Supplier", supplier)
            doc.check_permission("read")
        filters = {"disabled": 0}
        if supplier:
            filters["name"] = supplier
        or_filters = None
        if query:
            or_filters = {"name": ["like", f"%{query}%"],
                          "supplier_name": ["like", f"%{query}%"]}
        rows = frappe.get_list("Supplier", filters=filters, or_filters=or_filters,
            fields=["name", "supplier_name", "supplier_group", "supplier_type",
                    "country", "tax_id"], order_by="name asc", limit_start=start,
            limit_page_length=PAGE_SIZE + 1)
        return _page([dict(row) for row in rows], start,
                     reason="只返回启用供应商；请明确选择精确供应商后再办理采购。")
    except Exception as exc:
        return _failure(exc)


TOOLS = (
    ("get_inventory_options", "查询库存余额", False,
     '只读查询 ERPNext Bin 的库存余额、估值和库存单位。'),
    ("get_item_options", "查询物料档案", False,
     '只读查询启用物料及其库存、采购属性。'),
    ("get_supplier_options", "查询供应商档案", False,
     '只读查询启用供应商并返回准确编号。'),
)
