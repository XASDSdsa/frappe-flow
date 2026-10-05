"""Explicit delivery selection and order-scoped receipt evidence for Flow."""
from decimal import Decimal, InvalidOperation

import frappe


class DeliveryInputError(ValueError):
    def __init__(self, message, fields=None):
        super().__init__(message)
        self.fields = fields or []


def number(value, label="数量"):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) >= Decimal("1000000000"):
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise DeliveryInputError(f"{label}必须是有限有效数字。") from None


def _service_line(order, row):
    """Non-stock order lines finish delivery only when a delivery note includes them.

    Product-bundle parents are also non-stock, but their packed components are the
    goods being shipped, so they stay under the explicit selection.
    """
    bundled = {component.get("parent_detail_docname") for component in (order.get("packed_items") or [])}
    if row.name in bundled:
        return False
    return (not frappe.get_cached_value("Item", row.item_code, "is_stock_item")
            and not frappe.get_cached_value("Item", row.item_code, "is_fixed_asset"))


def available_rows(order, drafts):
    """Use native delivered_qty (already net of returns), then reserve drafts."""
    rows = []
    for row in order.get("items") or []:
        qty = number(row.qty)
        if qty <= 0 or row.get("delivered_by_supplier"):
            continue
        delivered = number(row.get("delivered_qty") or 0)
        if delivered < 0:
            raise DeliveryInputError("订单累计出库数量异常，请先核对退货记录。")
        remaining = max(qty - delivered, Decimal(0))
        if not remaining:
            continue
        occupied = sum((number(d["qty"]) for d in drafts if d["so_detail"] == row.name), Decimal(0))
        factor = number(row.get("conversion_factor"), "单位换算率")
        if factor <= 0 or occupied < 0:
            raise DeliveryInputError("单位换算率或出库草稿数量异常，请先处理原单。")
        rows.append({"row_no": row.idx, "sales_order_item": row.name, "item_code": row.item_code,
                     "item_name": row.item_name, "ordered_qty": float(qty), "delivered_qty": float(delivered),
                     "returned_qty": float(number(row.get("returned_qty") or 0)),
                     "remaining_qty": float(remaining), "draft_qty": float(occupied),
                     "available_qty": float(max(remaining - occupied, Decimal(0))),
                     "uom": row.uom, "stock_uom": row.stock_uom, "conversion_factor": float(factor),
                     "warehouse": row.warehouse, "rate": float(number(row.get("rate") or 0, "单价")),
                     "is_free_item": bool(row.get("is_free_item")),
                     "service": _service_line(order, row),
                     "whole_uom": bool(frappe.get_cached_value("UOM", row.uom, "must_be_whole_number")),
                     "whole_stock_uom": bool(frappe.get_cached_value("UOM", row.stock_uom, "must_be_whole_number"))})
    return rows


def select_rows(rows, items=None, all_remaining=False):
    """No selection is never interpreted as 'all'; identify duplicate SKUs by row."""
    if type(all_remaining) is not bool:
        raise DeliveryInputError("全部出库标记必须为布尔值。", ["all_remaining"])
    if all_remaining and items:
        raise DeliveryInputError("请使用全部出库或逐行选择其中一种方式。", ["items"])
    if not all_remaining and not items:
        raise DeliveryInputError("请先向客服列出可出库商品，等待其明确选择商品与数量，或回答全部出库。", ["items"])
    if items is not None and (not isinstance(items, list) or len(items) > 100):
        raise DeliveryInputError("逐行选择须为列表，最多100行。", ["items"])
    by_id = {row["sales_order_item"]: row for row in rows}
    by_no = {row["row_no"]: row for row in rows}
    choices = ([{"sales_order_item": r["sales_order_item"], "qty": r["available_qty"]}
                for r in rows if r["available_qty"] > 0] if all_remaining else list(items))
    if not all_remaining:
        # A service finishes the order only by riding a delivery note. Attach the
        # full remainder to the first explicit shipment unless that row was chosen.
        chosen = set()
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get("sales_order_item"):
                chosen.add(choice["sales_order_item"])
            elif choice.get("row_no") in by_no:
                chosen.add(by_no[choice["row_no"]]["sales_order_item"])
        for row in rows:
            if row.get("service") and row["available_qty"] > 0 and row["sales_order_item"] not in chosen:
                choices.append({"sales_order_item": row["sales_order_item"], "qty": row["available_qty"]})
    if len(choices) > 100:
        raise DeliveryInputError("一次最多选择100行，请分批出库。", ["items"])
    result, seen = [], set()
    for choice in choices:
        if not isinstance(choice, dict) or set(choice) - {"sales_order_item", "row_no", "qty"}:
            raise DeliveryInputError("请按展示的订单行号及数量选择；不要按可能重复的商品编码猜测。", ["items"])
        row_id, row_no = choice.get("sales_order_item"), choice.get("row_no")
        if row_id is not None and (not isinstance(row_id, str) or not row_id):
            raise DeliveryInputError("订单明细编号无效，请使用展示的行号。", ["items"])
        if row_no is not None and (type(row_no) is not int or row_no <= 0):
            raise DeliveryInputError("订单行号必须为正整数。", ["items"])
        row = by_id.get(row_id) if row_id else by_no.get(row_no)
        if not row or (row_no is not None and row_no != row["row_no"]):
            raise DeliveryInputError("所选行不在此次展示的可出库明细中，请重新选择。", ["items"])
        if row["sales_order_item"] in seen:
            raise DeliveryInputError("同一订单行被重复选择，请合并该行数量。", ["items"])
        qty = number(choice.get("qty"))
        if qty <= 0 or qty > number(row["available_qty"]):
            raise DeliveryInputError(f"第{row['row_no']}行 {row['item_name']} 本次可出库 {row['available_qty']:g} {row['uom']}，请选择范围内的正数。", ["items"])
        stock_qty = qty * number(row["conversion_factor"])
        if (row["whole_uom"] and qty != qty.to_integral_value()) or (row["whole_stock_uom"] and stock_qty != stock_qty.to_integral_value()):
            raise DeliveryInputError(f"第{row['row_no']}行数量不符合销售单位或库存单位的整数要求。", ["items"])
        result.append({**row, "qty": float(qty), "stock_qty": float(stock_qty)})
        seen.add(row["sales_order_item"])
    if not result:
        raise DeliveryInputError("剩余数量已出库或被其他草稿占用，请先处理原出库单。", ["sales_order"])
    return sorted(result, key=lambda row: row["row_no"])


def payment_status(total, paid, currency):
    total, paid = number(total, "订单金额"), number(paid, "已收款金额")
    if total < 0:
        raise DeliveryInputError("退货订单不能按普通出库单流程处理。")
    paid = max(paid, Decimal(0))
    remaining = max(total - paid, Decimal(0))
    status = "not_required" if total == 0 else "unpaid" if paid == 0 else "partial" if remaining > 0 else "paid"
    messages = {"not_required": "零金额订单，无需收款；仍须明确选品并整体审核。",
                "unpaid": "尚未查到有效收款，不能按已收款订单创建出库单。",
                "partial": "订单只收到部分款项，请重点核对未收余额；本次出库由客服整体批准后继续。",
                "paid": "订单已收足款项，请继续选择实际出库商品。"}
    return {"status": status, "currency": currency, "order_total": float(total), "paid": float(paid),
            "remaining": float(remaining), "eligible": status != "unpaid", "message": messages[status]}


def get_payment_summary(order, for_update=False):
    """Aggregate posted cash receipts only after the caller checks order read.

    Payment/Journal reference rows carry the original advance order and the
    current invoice. Select each allocation once with an OR and require posted
    GL evidence. Return only this order's aggregate to a sales user.
    """
    order.check_permission("read")
    lock = " for update" if for_update else ""
    invoice_rows = frappe.db.sql("""select distinct si.name from `tabSales Invoice` si
        inner join `tabSales Invoice Item` i on i.parent=si.name
        where i.sales_order=%s and si.company=%s and si.customer=%s and si.docstatus=1""" + lock,
        (order.name, order.company, order.customer), as_dict=True)
    invoices, ambiguous = [], []
    for invoice in invoice_rows:
        others = frappe.db.sql("""select name from `tabSales Invoice Item`
            where parent=%s and coalesce(sales_order,'')<>%s and (qty<>0 or amount<>0) limit 1""" + lock,
            (invoice.name, order.name))
        (ambiguous if others else invoices).append(invoice.name)
    params = {"order": order.name, "company": order.company, "customer": order.customer,
              "invoices": tuple(invoices) or ("",)}
    # In ERPNext v16 advance_voucher_* belong to payment/journal reference
    # rows, not GL Entry. Count each current allocation once and require posted
    # ledger evidence; an advance reconciled to an invoice is the same row.
    params["invoices"] = tuple(invoices + ambiguous) or ("",)
    entries = frappe.db.sql("""select r.name,'Payment Entry' as voucher_type,p.name as voucher_no,
        case when p.payment_type='Receive' then p.paid_from_account_currency else p.paid_to_account_currency end as account_currency,
        (case when p.payment_type='Receive' then r.allocated_amount
              when r.reference_doctype='Sales Invoice' then -abs(r.allocated_amount)
              else -r.allocated_amount end) as received,
        (case when p.payment_type='Receive' then r.allocated_amount*p.source_exchange_rate
              when r.reference_doctype='Sales Invoice' then -abs(r.allocated_amount)*p.target_exchange_rate
              else -r.allocated_amount*p.target_exchange_rate end) as base_received,
        r.advance_voucher_type,r.advance_voucher_no,
        r.reference_doctype as against_voucher_type,r.reference_name as against_voucher
        from `tabPayment Entry Reference` r inner join `tabPayment Entry` p on p.name=r.parent
        where p.docstatus=1 and p.company=%(company)s and p.party_type='Customer' and p.party=%(customer)s
        and p.payment_type in ('Receive','Pay')
        and ((r.advance_voucher_type='Sales Order' and r.advance_voucher_no=%(order)s)
          or (r.reference_doctype='Sales Order' and r.reference_name=%(order)s)
          or (r.reference_doctype='Sales Invoice' and r.reference_name in %(invoices)s))
        and exists (select 1 from `tabGL Entry` g inner join `tabAccount` a on a.name=g.account
            where g.voucher_type='Payment Entry' and g.voucher_no=p.name and g.is_cancelled=0
            and g.company=p.company and g.party_type='Customer' and g.party=p.party
            and g.account=case when p.payment_type='Receive' then p.paid_from else p.paid_to end
            and a.account_type='Receivable')""" + lock, params, as_dict=True)
    entries += frappe.db.sql("""select j.name,'Journal Entry' as voucher_type,p.name as voucher_no,j.account_currency,
        j.credit_in_account_currency-j.debit_in_account_currency as received,
        j.credit-j.debit as base_received,j.advance_voucher_type,j.advance_voucher_no,
        j.reference_type as against_voucher_type,j.reference_name as against_voucher
        from `tabJournal Entry Account` j inner join `tabJournal Entry` p on p.name=j.parent
        inner join `tabAccount` a on a.name=j.account
        where p.docstatus=1 and p.company=%(company)s and j.party_type='Customer' and j.party=%(customer)s
        and a.account_type='Receivable'
        and ((j.advance_voucher_type='Sales Order' and j.advance_voucher_no=%(order)s)
          or (j.reference_type='Sales Order' and j.reference_name=%(order)s)
          or (j.reference_type='Sales Invoice' and j.reference_name in %(invoices)s))
        and exists (select 1 from `tabGL Entry` g where g.voucher_type='Journal Entry'
            and g.voucher_no=p.name and g.is_cancelled=0 and g.company=p.company
            and g.party_type='Customer' and g.party=j.party and g.account=j.account)""" + lock, params, as_dict=True)
    company_currency = frappe.get_cached_value("Company", order.company, "default_currency")
    paid, checked, seen = Decimal(0), {}, set()
    for row in entries:
        allocation = (row.voucher_type, row.name)
        if allocation in seen:
            continue
        seen.add(allocation)
        if row.get("against_voucher_type") == "Sales Invoice" and row.get("against_voucher") in ambiguous and not (
            row.get("advance_voucher_type") == "Sales Order" and row.get("advance_voucher_no") == order.name
        ):
            raise DeliveryInputError("该订单涉及多订单合并发票收款，无法确定分配到本单的实际金额，请先由财务核对收款归属。", ["payment_allocation"])
        key = (row.voucher_type, row.voucher_no)
        if key not in checked:
            valid = frappe.db.get_value(row.voucher_type, row.voucher_no, "docstatus", for_update=for_update) == 1
            if valid and row.voucher_type == "Journal Entry":
                valid = bool(frappe.db.sql("""select j.name from `tabJournal Entry Account` j
                    inner join `tabAccount` a on a.name=j.account
                    where j.parent=%s and a.account_type in ('Bank','Cash')
                    and (j.debit_in_account_currency<>0 or j.credit_in_account_currency<>0) limit 1""" + lock, row.voucher_no))
            checked[key] = valid
        if not checked[key]:
            continue
        if row.get("advance_voucher_type") == "Sales Order":
            if row.get("advance_voucher_no") != order.name or (
                row.get("against_voucher_type") == "Sales Invoice" and row.get("against_voucher") not in invoices + ambiguous
            ):
                raise DeliveryInputError("存在跨订单预收款转入或转出的收款记录，请财务核对本单收款归属后再出库。", ["payment_allocation"])
        if row.account_currency == order.currency:
            paid += number(row.received, "收款金额")
        elif order.currency == company_currency:
            paid += number(row.base_received, "本位币收款金额")
        elif row.account_currency == company_currency and order.get("party_account_currency") == company_currency:
            rate = number(order.get("conversion_rate"), "订单汇率")
            if rate <= 0:
                raise DeliveryInputError("订单缺少有效汇率，不能核实收款金额。", ["payment_currency"])
            paid += number(row.base_received, "本位币收款金额") / rate
        else:
            raise DeliveryInputError("收款往来币种与订单不同，不能直接比较金额，请先核对收款币种及归属。", ["payment_currency"])
    precision = order.precision("grand_total")
    quantum = Decimal(1).scaleb(-precision)
    total = order.grand_total if order.get("disable_rounded_total") or not order.get("rounded_total") else order.rounded_total
    result = payment_status(number(total).quantize(quantum), paid.quantize(quantum), order.currency)
    result["basis"] = "本单关联、已提交且未取消的有效收款；已排除无银行/现金科目的日记账核销，退款抵减。"
    return result
