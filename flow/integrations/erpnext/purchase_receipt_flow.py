"""Reviewed, permission-preserving native purchase receipts for actual arrivals.

The shared reviewed_flow boundary owns rollback, approval and idempotency. This
module only reads sources and builds/validates unsaved native documents.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import math

import frappe
from frappe.utils import nowdate
from flow.lib.tool import tool

from .reviewed_flow import InputError, confirmation, execute, preview
from .sales_order_flow import _without_price_maintenance

KIND = "purchase_receipt"
DEFAULT_WAREHOUSE = "大坪仓库 - LEYA"
REQUEST_FIELDS = {
    "purchase_order", "source_revision", "supplier", "company", "currency", "customer",
    "posting_date", "supplier_reference", "supplier_invoice", "warehouse", "items",
    "existing_document", "submit", "actual_receipt", "new_request",
}
EXISTING_FIELDS = {"existing_document", "submit", "actual_receipt", "new_request"}


def _number(value, label, *, zero=False):
    if isinstance(value, bool) or value is None or value == "":
        raise InputError(f"{label}必须是明确的有限{'非负' if zero else '正'}数。", [label])
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise InputError(f"{label}必须是明确的有限数字。", [label]) from None
    if not math.isfinite(result) or (result < 0 if zero else result <= 0):
        raise InputError(f"{label}必须是有限{'非负' if zero else '正'}数，不能用零估值代替真实成本。", [label])
    return result


def _text(value, field, *, optional=False):
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InputError(f"{field}须提供精确编号或文字，不能使用空值或带首尾空格的近似名称。", [field])
    return value


def _read(doctype, name, *, for_update=False):
    doc = frappe.get_doc(doctype, name, for_update=for_update)
    doc.check_permission("read")
    return doc


def _failure(exc):
    return {"status": "needs_input" if isinstance(exc, InputError) else "error",
            "reason": str(exc), "missing": getattr(exc, "fields", []), "verified": False}


def _request(request):
    if not isinstance(request, dict):
        raise InputError("请提供采购收货 request 对象。", ["request"])
    result = dict(request)
    unknown = set(result) - REQUEST_FIELDS
    if unknown:
        raise InputError("不支持的收货字段：" + "、".join(sorted(unknown)), sorted(unknown))
    for key in ("submit", "actual_receipt", "new_request"):
        if key in result and type(result[key]) is not bool:
            raise InputError(key + " 必须为 true 或 false。", [key])
    result.setdefault("submit", False)
    if result["submit"] and result.get("actual_receipt") is not True:
        raise InputError("提交入库必须由用户明确确认本批实际已到货；请核对后提供 actual_receipt=true，再整体审核批准。", ["actual_receipt"])
    if result.get("existing_document"):
        extra = set(result) - EXISTING_FIELDS
        if extra:
            raise InputError("已有草稿按当前原单完整审核，不接受隐藏覆盖；请先在原单修改后重新预检。", sorted(extra))
        _text(result["existing_document"], "existing_document")
        return result
    missing, reasons = [], []
    if not result.get("purchase_order") and not result.get("supplier"):
        missing.append("supplier")
        reasons.append("直接收货须提供实际供应商编号，或提供来源采购订单")
    rows = result.get("items")
    if not isinstance(rows, list) or not rows or len(rows) > 100:
        missing.append("items")
        reasons.append("请逐行明确本次实际收货明细，单次支持 1 至 100 行")
    else:
        allowed = {"purchase_order_item", "item_code", "qty", "rate", "warehouse", "uom"}
        seen = set()
        for index, row in enumerate(rows, 1):
            prefix = f"items[{index}]"
            if not isinstance(row, dict):
                missing.append(prefix)
                reasons.append(f"第 {index} 行须是明细对象")
                continue
            extras = set(row) - allowed
            if extras:
                missing.extend(prefix + "." + key for key in sorted(extras))
                reasons.append(f"第 {index} 行含不支持字段：" + "、".join(sorted(extras)))
            key = "purchase_order_item" if result.get("purchase_order") else "item_code"
            try:
                identity = _text(row.get(key), key)
                if identity in seen:
                    raise InputError("来源采购行或直接收货物料不可重复；请合并明确的实际数量。")
                seen.add(identity)
            except InputError as exc:
                missing.append(prefix + "." + key)
                reasons.append(f"第 {index} 行：{exc}")
            if not result.get("purchase_order") and row.get("purchase_order_item"):
                missing.append(prefix + ".purchase_order_item")
                reasons.append(f"第 {index} 行提供了采购行但缺少 purchase_order")
            for field in ("qty", "rate"):
                if field == "rate" and result.get("purchase_order") and field not in row:
                    continue
                try:
                    _number(row.get(field), "实际合格收货数量" if field == "qty" else "实际采购单位成本")
                except InputError as exc:
                    missing.append(prefix + "." + field)
                    reasons.append(f"第 {index} 行：{exc}")
            for field in ("item_code", "warehouse", "uom"):
                if field in row:
                    try:
                        _text(row[field], field)
                    except InputError as exc:
                        missing.append(prefix + "." + field)
                        reasons.append(f"第 {index} 行：{exc}")
    if missing:
        raise InputError("请集中补齐或修正：" + "；".join(reasons), missing)
    for key in ("purchase_order", "source_revision", "supplier", "company", "currency", "customer",
                "warehouse", "supplier_reference", "supplier_invoice"):
        if key in result:
            _text(result[key], key, optional=True)
    result["posting_date"] = result.get("posting_date") or nowdate()
    try:
        actual_date = date.fromisoformat(_text(result["posting_date"], "posting_date"))
    except (ValueError, InputError):
        raise InputError("实际收货日期须为 YYYY-MM-DD。", ["posting_date"]) from None
    if str(actual_date) != result["posting_date"] or actual_date > date.fromisoformat(nowdate()):
        raise InputError("实际收货日期不能是未来日期，须使用 YYYY-MM-DD。", ["posting_date"])
    return result


def _available_rows(order, drafts):
    """PO received_qty already includes submitted receipt/invoice arrivals."""
    reserved = defaultdict(float)
    rows = {row.name: row for row in order.get("items") or []}
    for draft in drafts:
        source = rows.get(draft.get("purchase_order_item"))
        if source is None:
            raise InputError("已有收货草稿的采购行关联异常，请先核对原单。", ["purchase_order"])
        if draft.get("item_code") != source.item_code or draft.get("uom") != source.uom:
            raise InputError("已有收货草稿与采购行物料或单位不一致，请先处理原草稿。", ["purchase_order"])
        accepted = _number(draft.get("qty"), "已有草稿合格数量", zero=True)
        qty = _number(draft.get("received_qty") or accepted, "已有草稿数量", zero=True)
        rejected = _number(draft.get("rejected_qty") or 0, "已有草稿拒收数量", zero=True)
        if not draft.get("received_qty"):
            qty += rejected
        elif abs(qty - accepted - rejected) > 0.000001:
            raise InputError("已有收货草稿的收货、合格及拒收数量不一致，请先核对原单。", ["purchase_order"])
        reserved[source.name] += qty
    result = []
    for row in order.get("items") or []:
        ordered = _number(row.qty, "采购数量", zero=True)
        received = _number(row.get("received_qty") or 0, "已收货数量", zero=True)
        blocked = "供应商直送行须使用原生直送流程。" if row.get("delivered_by_supplier") else ""
        if not ordered:
            blocked = "仅单价采购行没有确定收货上限，请在原生页面确认实际数量。"
        result.append({"purchase_order_item": row.name, "row_no": row.idx, "item_code": row.item_code,
            "item_name": row.item_name, "uom": row.uom, "ordered_qty": ordered,
            "received_qty": received, "draft_qty": reserved[row.name],
            "available_qty": 0.0 if blocked else max(0.0, ordered - received - reserved[row.name]),
            "rate": _number(row.rate, "采购订单单位成本", zero=True),
            "warehouse": row.get("warehouse") or DEFAULT_WAREHOUSE, "blocked_reason": blocked})
    return result


def _order_state(name, *, for_update=False, exclude_receipt=None):
    order = _read("Purchase Order", name, for_update=for_update)
    if order.docstatus != 1 or order.status in ("Closed", "Cancelled", "On Hold", "Completed"):
        raise InputError("采购订单须已提交、有效且仍待收货；请核对订单状态。", ["purchase_order"])
    if order.get("is_subcontracted") or order.get("is_old_subcontracting_flow"):
        raise InputError("委外采购须通过原生委外收货流程处理实际用料。", ["purchase_order"])
    lock = " for update" if for_update else ""
    drafts = frappe.db.sql("""select i.purchase_order_item, i.item_code, i.uom, i.qty,
        i.received_qty, i.rejected_qty from `tabPurchase Receipt Item` i
        inner join `tabPurchase Receipt` d on d.name=i.parent
        where i.purchase_order=%s and d.docstatus=0 and coalesce(d.is_return,0)=0
        and d.name<>%s order by d.name,i.idx""" + lock, (name, exclude_receipt or ""), as_dict=True)
    invoice_drafts = frappe.db.sql("""select i.po_detail as purchase_order_item,
        i.item_code, i.uom, i.qty, i.received_qty, i.rejected_qty from `tabPurchase Invoice Item` i
        inner join `tabPurchase Invoice` d on d.name=i.parent
        where i.purchase_order=%s and d.docstatus=0 and d.update_stock=1
        and coalesce(d.is_return,0)=0 order by d.name,i.idx""" + lock, (name,), as_dict=True)
    return order, _available_rows(order, list(drafts) + list(invoice_drafts))


def _supplier(name):
    supplier = _read("Supplier", name)
    if supplier.get("disabled") or supplier.get("is_internal_supplier"):
        raise InputError("供应商已停用或属于内部调拨供应商，请核对并使用相应原生流程。", ["supplier"])
    return supplier


def _item(code, customer=None):
    from erpnext.stock.doctype.item.sticker_stock import is_customer_sticker, sticker_customer

    item = _read("Item", code)
    if item.get("disabled") or item.get("has_variants") or not item.get("is_purchase_item"):
        raise InputError(f"物料 {code} 须是启用且允许采购的明确物料，不能选择变体模板。", ["items"])
    if item.get("has_serial_no") or item.get("has_batch_no"):
        raise InputError(f"物料 {code} 需要真实序列号或批次，请在原生采购收货页面完成。", ["items"])
    if item.get("is_fixed_asset") or item.get("is_sub_contracted_item"):
        raise InputError(f"物料 {code} 涉及资产或委外，须在原生页面补齐实际业务资料。", ["items"])
    owner = ""
    if is_customer_sticker(item):
        if not item.get("is_stock_item"):
            raise InputError(f"客户贴纸 {code} 必须维护库存，请先修复物料档案。", ["items"])
        owner = sticker_customer(item)  # Exact established identity, including legacy codes; checks Customer read.
        if customer and owner != customer:
            raise InputError(f"客户贴纸 {code} 不属于指定客户，请核对精确贴纸编号。", ["customer", "items"])
    return item, owner


def _warehouse(name, company):
    doc = _read("Warehouse", name)
    if doc.company != company or doc.get("disabled") or doc.get("is_group"):
        raise InputError("收货仓库须为本公司启用的实际仓库。", ["warehouse", "company"])
    return doc.name


def _company(explicit):
    name = explicit or frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
    if not name:
        choices = frappe.get_list("Company", pluck="name", limit_page_length=2)
        name = choices[0] if len(choices) == 1 else None
    if not name:
        raise InputError("没有唯一的默认公司，请提供本次收货公司。", ["company"])
    return _read("Company", name)


def _select(rows, requested):
    available = {row["purchase_order_item"]: row for row in rows}
    selected = []
    seen = set()
    for choice in requested:
        key = choice["purchase_order_item"]
        if key in seen:
            raise InputError("同一采购订单行不能重复收货，请合并数量。", ["items"])
        seen.add(key)
        row = available.get(key)
        if not row or choice.get("item_code", row["item_code"]) != row["item_code"]:
            raise InputError("采购订单行编号或物料不匹配，请重新读取明细。", ["items"])
        if choice.get("uom", row["uom"]) != row["uom"]:
            raise InputError("采购来源行须使用原采购单位，请核对实际数量。", ["items"])
        qty = _number(choice["qty"], "实际合格收货数量")
        if row["blocked_reason"] or qty > row["available_qty"] + 0.000001:
            raise InputError(row["blocked_reason"] or
                f"采购第 {row['row_no']} 行当前最多可收 {row['available_qty']:g} {row['uom']}（已扣除其他草稿）；请重新核对数量。", ["items"])
        selected.append({**row, "qty": qty, "rate": _number(choice.get("rate", row["rate"]), "实际采购单位成本"),
                         "warehouse": choice.get("warehouse")})
    return selected


def _check_document(doc, request):
    if doc.get("is_return") or doc.get("is_subcontracted") or doc.get("subcontracting_receipt") or doc.get("is_internal_supplier"):
        raise InputError("退货、委外或内部调拨收货须在对应原生流程处理。", ["existing_document"])
    if doc.get("apply_putaway_rule"):
        raise InputError("此单启用了自动上架分仓，请在原生页面审核实际仓库分配。", ["warehouse"])
    if not doc.get("items"):
        raise InputError("收货单没有实际明细。", ["items"])
    seen = set()
    for row in doc.get("items"):
        if bool(row.get("purchase_order")) != bool(row.get("purchase_order_item")):
            raise InputError("采购订单与精确来源行必须同时存在，请修复收货明细的采购关联。", ["items"])
        if row.get("purchase_order_item"):
            if row.purchase_order_item in seen:
                raise InputError("同一采购行在收货单中重复，请先核对并合并实际数量。", ["items"])
            seen.add(row.purchase_order_item)
        if any(row.get(key) for key in ("serial_no", "batch_no", "serial_and_batch_bundle", "rejected_qty",
                "landed_cost_voucher_amount", "from_warehouse", "purchase_invoice", "purchase_invoice_item")):
            raise InputError("收货明细涉及批次序列号、拒收、后加成本、仓间转移或采购发票关联，请在原生页面处理。", ["items"])
        if row.get("allow_zero_valuation_rate"):
            raise InputError("不能通过零估值许可入库，请补齐真实采购成本。", ["items"])
        item, _ = _item(row.item_code, request.get("customer"))
        _number(row.qty, "实际合格收货数量")
        _number(row.rate, "实际采购单位成本")
        _number(row.conversion_factor, "库存单位换算")
        if row.get("received_qty") and abs(float(row.received_qty) - float(row.qty)) > 0.000001:
            raise InputError("实际收货数与合格数量不一致，请在原生页面核对拒收明细。", ["items"])
        if item.get("is_stock_item"):
            _warehouse(row.warehouse, doc.company)
        if row.get("sales_order"):
            sale = _read("Sales Order", row.sales_order)
            if sale.company != doc.company:
                raise InputError("收货关联销售订单公司不一致。", ["items"])
            _item(row.item_code, sale.customer)


def _validate_native(doc, request):
    doc._action = "save"
    doc.flags.ignore_permissions = False
    _check_document(doc, request)
    before = [(r.item_code, float(r.qty), float(r.rate), r.warehouse, r.uom,
               float(r.conversion_factor), r.get("purchase_order"), r.get("purchase_order_item")) for r in doc.get("items")]
    doc.run_method("before_validate")
    doc.run_method("validate")
    original_name = doc.name
    if not original_name:
        doc.name = "new-flow-purchase-receipt-preview"
        doc.set_parent_in_children()
    try:
        doc._validate()
    finally:
        doc.name = original_name
        doc.set_parent_in_children()
    after = [(r.item_code, float(r.qty), float(r.rate), r.warehouse, r.uom,
              float(r.conversion_factor), r.get("purchase_order"), r.get("purchase_order_item")) for r in doc.get("items")]
    if before != after:
        raise InputError("原生校验改变了已选物料、数量、单价、单位、仓库或采购行关联，请核对后重新预检。", ["items"])
    _check_document(doc, request)
    _number(doc.conversion_rate, "公司本位币汇率")
    for row in doc.get("items"):
        if frappe.get_cached_value("Item", row.item_code, "is_stock_item"):
            _number(row.valuation_rate, "实际库存单位估值")
    return doc


def _existing(request, for_update):
    name = request["existing_document"]
    initial = _read("Purchase Receipt", name)
    source_names = sorted({r.purchase_order for r in initial.get("items") or [] if r.get("purchase_order")})
    sources = {source: _order_state(source, for_update=for_update, exclude_receipt=name) for source in source_names}
    doc = _read("Purchase Receipt", name, for_update=for_update)
    if doc.docstatus != 0:
        raise InputError("仅可审核保存或提交尚未提交的采购收货草稿。", ["existing_document"])
    doc.check_permission("write")
    if request["submit"]:
        doc.check_permission("submit")
    _supplier(doc.supplier)
    _read("Company", doc.company)
    if {r.purchase_order for r in doc.get("items") or [] if r.get("purchase_order")} != set(sources):
        raise InputError("锁定期间原单采购来源已变化，请重新预检。", ["existing_document"])
    for source, (order, rows) in sources.items():
        if (doc.supplier, doc.company, doc.currency) != (order.supplier, order.company, order.currency):
            raise InputError("草稿与来源采购订单的供应商、公司或币种不一致。", ["existing_document"])
        _select(rows, [{"purchase_order_item": r.purchase_order_item, "item_code": r.item_code,
                       "qty": r.qty, "uom": r.uom, "rate": r.rate}
                      for r in doc.get("items") if r.get("purchase_order") == source])
    return _validate_native(doc, request)


@_without_price_maintenance()
def _build(request, for_update=False):
    request = _request(request)
    if request.get("existing_document"):
        return _existing(request, for_update)
    if request.get("customer"):
        _read("Customer", request["customer"])
    order, selected = None, []
    if request.get("purchase_order"):
        order, rows = _order_state(request["purchase_order"], for_update=for_update)
        if request.get("source_revision") and str(order.modified) != request["source_revision"]:
            raise InputError("采购订单在展示或审核后已变化，请重新读取明细并预检。", ["purchase_order"])
        for field in ("supplier", "company", "currency"):
            if request.get(field) and request[field] != order.get(field):
                raise InputError(f"指定 {field} 与采购订单不一致。", [field])
        selected = _select(rows, request["items"])
        supplier = _supplier(order.supplier)
        company = _read("Company", order.company)
        # Check sensitive masters before calling the native mapper.
        for chosen in selected:
            _item(chosen["item_code"], request.get("customer"))
        from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
        doc = make_purchase_receipt(order.name, args={"filtered_children": [r["purchase_order_item"] for r in selected]})
        if {r.purchase_order_item for r in doc.get("items")} != {r["purchase_order_item"] for r in selected} or len(doc.get("items")) != len(selected):
            raise InputError("原生转换明细与明确选择不一致，请重新读取采购订单。", ["items"])
        choices = {r["purchase_order_item"]: r for r in selected}
        for row in doc.get("items"):
            chosen = choices[row.purchase_order_item]
            row.qty = row.received_qty = chosen["qty"]
            row.rate = chosen["rate"]
            row.warehouse = chosen["warehouse"] or request.get("warehouse") or row.warehouse or DEFAULT_WAREHOUSE
    else:
        supplier = _supplier(request["supplier"])
        company = _company(request.get("company"))
        doc = frappe.new_doc("Purchase Receipt")
        doc.supplier, doc.company = supplier.name, company.name
        doc.currency = request.get("currency") or supplier.get("default_currency") or company.default_currency
        if not doc.currency:
            raise InputError("缺少实际结算币种，请提供 currency。", ["currency"])
        for chosen in request["items"]:
            item, _ = _item(chosen["item_code"], request.get("customer"))
            uom = chosen.get("uom") or item.stock_uom
            factors = [r.conversion_factor for r in item.get("uoms") or [] if r.uom == uom]
            factor = 1.0 if uom == item.stock_uom else _number(factors[0], "库存单位换算") if len(factors) == 1 else None
            if not factor:
                raise InputError(f"物料 {item.name} 未配置唯一的 {uom} 库存单位换算。", ["items"])
            doc.append("items", {"item_code": item.name, "qty": _number(chosen["qty"], "实际收货数量"),
                "received_qty": _number(chosen["qty"], "实际收货数量"), "rate": _number(chosen["rate"], "实际采购单位成本"),
                "uom": uom, "stock_uom": item.stock_uom, "conversion_factor": factor,
                "warehouse": chosen.get("warehouse") or request.get("warehouse") or DEFAULT_WAREHOUSE})
    doc.check_permission("create")
    if request["submit"]:
        doc.check_permission("submit")
    doc.posting_date = request["posting_date"]
    doc.ignore_pricing_rule = 1
    if request.get("supplier_reference"):
        doc.supplier_delivery_note = request["supplier_reference"]
    if request.get("supplier_invoice"):
        doc.remarks = "供应商发票：" + request["supplier_invoice"] + ("\n" + doc.remarks if doc.get("remarks") else "")
    _check_document(doc, request)
    expected_rates = [float(row.rate) for row in doc.get("items")]
    doc.run_method("set_missing_values")
    if [float(row.rate) for row in doc.get("items")] != expected_rates:
        raise InputError("原生定价改变了实际采购单价，请核对价格规则后重新预检。", ["items"])
    doc.set_qty_as_per_stock_uom()
    doc.calculate_taxes_and_totals()
    return _validate_native(doc, request)


def _summary(doc, request):
    items = []
    for row in doc.get("items"):
        _, customer = _item(row.item_code, request.get("customer"))
        items.append({"采购订单": row.get("purchase_order") or "", "采购行": row.get("purchase_order_item") or "",
            "物料编号": row.item_code, "物料名称": row.item_name, "贴纸所属客户": customer,
            "合格收货数量": float(row.qty), "采购单位": row.uom, "库存数量": float(row.stock_qty),
            "库存单位": row.stock_uom, "收货仓库": row.warehouse or "", "实际单价": float(row.rate),
            "净金额": float(row.net_amount), "本位币库存单位估值": float(row.valuation_rate or 0)})
    taxes = [{"类型": r.charge_type, "账户": r.account_head, "税率": float(r.rate or 0),
              "金额": float(r.tax_amount or 0), "用途": r.category, "增减": r.add_deduct_tax}
             for r in doc.get("taxes") or []]
    return {"title": "采购收货", "action": "提交实际采购入库" if request.get("submit") else "保存采购收货草稿",
        "effects": ["提交原生采购收货单，按原生设置更新库存及相关会计账；不会登记供应商付款。"] if request.get("submit")
                   else ["保存草稿，尚未增加库存或记账。"],
        "fields": {"已有草稿": request.get("existing_document") or "", "供应商": doc.supplier,
            "供应商名称": doc.supplier_name, "公司": doc.company, "币种": doc.currency,
            "本位币汇率": float(doc.conversion_rate), "实际收货日期": str(doc.posting_date),
            "供应商送货或参考号": doc.get("supplier_delivery_note") or "", "备注及供应商发票号": doc.get("remarks") or "",
            "税费模板": doc.get("taxes_and_charges") or "", "税费明细": taxes},
        "items": items, "totals": {"净额": float(doc.net_total), "税费": float(doc.total_taxes_and_charges or 0),
            "总额": float(doc.grand_total), "舍入总额": float(doc.rounded_total or 0),
            "禁用舍入": bool(doc.disable_rounded_total), "本位币净额": float(doc.base_net_total)},
        "warnings": ["供应商发票号仅记入收货备注，不会创建采购发票。"] if request.get("supplier_invoice") else []}


def get_purchase_receipt_options(purchase_order: str = "", supplier: str = "",
                                 item_codes: list[str] | None = None, start: int = 0):
    """只读列出采购订单待收货数量或直接收货物料；库存与成本仅在实际到货并提交后形成。"""
    try:
        if type(start) is not int or start < 0:
            raise InputError("start 必须为非负整数。", ["start"])
        if supplier:
            _supplier(_text(supplier, "supplier"))
        if purchase_order:
            order, rows = _order_state(_text(purchase_order, "purchase_order"))
            if supplier and supplier != order.supplier:
                raise InputError("供应商与采购订单不匹配。", ["supplier"])
            _supplier(order.supplier)
            actionable = []
            for row in rows:
                try:
                    _, owner = _item(row["item_code"])
                    row["customer"] = owner
                    if row["rate"] <= 0:
                        row["blocked_reason"] = "来源采购单价缺失；须提供真实正数单位成本。"
                except InputError as exc:
                    row["blocked_reason"] = str(exc)
                actionable.append(row)
            return {"status": "needs_selection" if any(r["available_qty"] > 0 and not r["blocked_reason"] for r in actionable) else "blocked",
                "reason": "请按采购行编号明确本次实际合格数量；可收数量已扣除已收货和其他收货/更新库存发票草稿。",
                "purchase_order": order.name, "source_revision": str(order.modified), "supplier": order.supplier,
                "company": order.company, "currency": order.currency, "items": actionable, "default_warehouse": DEFAULT_WAREHOUSE}
        if item_codes is not None:
            if not isinstance(item_codes, list) or not item_codes or len(item_codes) > 100:
                raise InputError("item_codes 须为 1 至 100 个精确物料编号。", ["item_codes"])
            results = []
            for code in item_codes:
                item, owner = _item(_text(code, "item_codes"))
                results.append({"item_code": item.name, "item_name": item.item_name, "stock_uom": item.stock_uom,
                    "customer": owner, "is_stock_item": bool(item.is_stock_item),
                    "reason": "请提供本次实际数量、真实单位成本及实际仓库。"})
            return {"status": "needs_input", "reason": "直接收货的实际采购成本不从售价猜测。", "supplier": supplier,
                "items": results, "missing": ([] if supplier else ["supplier"]) + ["items.qty", "items.rate"],
                "default_warehouse": DEFAULT_WAREHOUSE}
        filters = {"docstatus": 1, "status": ["not in", ["Closed", "Cancelled", "On Hold", "Completed"]]}
        if supplier:
            filters["supplier"] = supplier
        orders = frappe.get_list("Purchase Order", filters=filters,
            fields=["name", "supplier", "supplier_name", "company", "currency", "transaction_date", "per_received"],
            order_by="transaction_date desc, name desc", limit_start=start, limit_page_length=21)
        return {"status": "needs_selection" if orders else "needs_input", "orders": [dict(r) for r in orders[:20]],
            "has_more": len(orders) > 20, "next_start": start + 20 if len(orders) > 20 else None,
            "reason": "请选择实际到货对应的采购订单，或提供供应商、精确物料、实际数量及单位成本进行直接收货。"}
    except Exception as exc:
        return _failure(exc)


def preview_purchase_receipt(request: dict):
    """预检 request，不保存。新单 items 每行 qty 加 purchase_order_item（采购来源）或 item_code/rate（直接收货）。

    可选 purchase_order、supplier、company、currency、customer、posting_date、warehouse、supplier_reference、supplier_invoice。
    直接收货 rate 必须为真实正数单位成本；采购来源可沿用原采购单价。仓库默认大坪仓库 - LEYA，日期默认今天。
    默认 submit=false；仅用户明确本批实际到货并要求提交时同时传 submit=true、actual_receipt=true。
    已有草稿仅传 existing_document 和上述布尔项，完整审核当前原单；不接受覆盖字段。
    返回 preview_token 后调用 save_purchase_receipt，使用系统整单审核卡片批准一次。
    """
    try:
        request = _request(request)
        if request.get("purchase_order") and not request.get("source_revision"):
            request["source_revision"] = str(_read("Purchase Order", request["purchase_order"]).modified)
    except Exception as exc:
        return _failure(exc)
    return preview(KIND, request, _build, _summary)


def _verify(doc, request):
    if not request.get("submit"):
        return
    import erpnext
    from frappe.utils import flt
    stock_rows = [row for row in doc.items if frappe.get_cached_value("Item", row.item_code, "is_stock_item")]
    for row in stock_rows:
        entries = frappe.get_all("Stock Ledger Entry", filters={"voucher_type": "Purchase Receipt", "voucher_no": doc.name,
            "voucher_detail_no": row.name, "is_cancelled": 0}, fields=["company", "item_code", "warehouse", "actual_qty", "stock_value_difference"])
        if (not entries or any(e.company != doc.company or e.item_code != row.item_code or e.warehouse != row.warehouse or flt(e.actual_qty) <= 0 for e in entries)
                or abs(sum(flt(e.actual_qty) for e in entries) - flt(row.stock_qty)) > 0.000001
                or any(not math.isfinite(flt(e.stock_value_difference)) for e in entries)
                or sum(flt(e.stock_value_difference) for e in entries) <= 0):
            raise InputError("提交后的真实入库流水与数量、仓库或正成本不一致，本次入库须撤回。")
    if stock_rows and erpnext.is_perpetual_inventory_enabled(doc.company):
        entries = frappe.get_all("GL Entry", filters={"voucher_type": "Purchase Receipt", "voucher_no": doc.name,
            "is_cancelled": 0}, fields=["company", "debit", "credit"])
        if (not entries or any(e.company != doc.company for e in entries)
                or abs(sum(flt(e.debit) - flt(e.credit) for e in entries)) > 0.01):
            raise InputError("入库会计流水缺失、公司错误或借贷不平，本次入库须撤回。")


@tool(requires_confirmation=True, confirm_prompt=lambda args: confirmation(KIND, args.get("preview_token")))
def save_purchase_receipt(preview_token: str):
    """整单批准后按预检保存或提交原生采购收货；重试返回本次已有单据，提交才实际入库记账。"""
    with _without_price_maintenance():
        return execute(KIND, preview_token, _build, _summary, verify=_verify)


TOOLS = [
    ("get_purchase_receipt_options", "查询实际采购收货来源", False, "查询采购订单剩余数量或精确物料，包含草稿占用与阻止原因，不增加库存。"),
    ("preview_purchase_receipt", "预检实际采购收货", False, "核对实际供应商、采购行、数量、真实成本和仓库；返回整单审核方案。"),
    ("save_purchase_receipt", "保存或提交实际采购收货", True, "整单批准一次后保存草稿；明确实际到货且要求提交时按原生流程入库记账。"),
]
HINT = "采购收货：先查询精确来源或物料，再集中补齐实际数量和成本。只在明确实际到货并要求提交时使用 submit=true 与 actual_receipt=true。默认草稿；客户贴纸须属于实际客户、维护库存且具有真实正成本。保存工具整单批准一次，禁止另造确认开关或自动选择全部剩余数量。"
