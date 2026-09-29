"""Business regressions for explicit delivery selection and actual cash receipts."""
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest


class Row(dict):
    __getattr__ = dict.get


@pytest.fixture
def harness(monkeypatch):
    fake = types.ModuleType("frappe")
    fake.get_cached_value = lambda dt, name, field: "CNY" if dt == "Company" else name in ("Nos", "Box")
    state = Row(entries=[], invoices=[], mixed=False, uncertain=False, cancelled=set(), noncash=set(), queries=[])

    def sql(query, args=None, **kwargs):
        state.queries.append((query, args))
        if "select distinct si.name" in query:
            return [Row(name=n) for n in state.invoices]
        if "select name from `tabSales Invoice Item`" in query:
            return [("other-row",)] if state.mixed else []
        if "from `tabPayment Entry Reference`" in query:
            rows = [r for r in state.entries if r.voucher_type == "Payment Entry"]
            if state.uncertain:
                rows.append(receipt(against_voucher_type="Sales Invoice", against_voucher="MIXED-INV"))
            return rows
        if "from `tabJournal Entry Account` j inner join `tabJournal Entry`" in query:
            return [r for r in state.entries if r.voucher_type == "Journal Entry"]
        if "from `tabJournal Entry Account`" in query:
            return [] if args in state.noncash else [("bank-leg",)]
        raise AssertionError(query)

    fake.db = Row(sql=sql, get_value=lambda dt, name, field, **kw: 2 if name in state.cancelled else 1)
    monkeypatch.setitem(sys.modules, "frappe", fake)
    path = Path(__file__).resolve().parents[2] / "flow/integrations/erpnext/delivery_note_rules.py"
    spec = importlib.util.spec_from_file_location("delivery_rules_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    order = Row(name="SO-1", company="Co", customer="Customer-1", currency="USD", grand_total=100,
                rounded_total=100, conversion_rate=7, party_account_currency="USD", precision=lambda *_: 2,
                check_permission=Mock())
    return module, fake, state, order


def line(name="row-a", idx=1, **updates):
    return Row(name=name, idx=idx, item_code="SAME-SKU", item_name="商品", qty=10, delivered_qty=0,
               returned_qty=0, uom="Nos", stock_uom="Nos", conversion_factor=1, warehouse="大坪仓库 - LEYA",
               rate=3, is_free_item=0, **updates)


def stock_rows(module, *rows, drafts=None):
    return module.available_rows(Row(items=list(rows) or [line()]), drafts or [])


def receipt(name="GL-1", voucher="PE-1", amount=100, currency="USD", kind="Payment Entry", **updates):
    return Row(name=name, voucher_type=kind, voucher_no=voucher, account_currency=currency,
               received=amount, base_received=amount * 7, **updates)


def test_no_selection_is_not_all(harness):
    m, *_ = harness
    with pytest.raises(m.DeliveryInputError, match="等待.*明确选择"):
        m.select_rows(stock_rows(m))


def test_partial_selection_does_not_add_free_sticker(harness):
    m, *_ = harness
    sticker = line("sticker", 2)
    sticker.update(item_code="STICKER", item_name="客户贴纸", rate=0, is_free_item=1)
    selected = m.select_rows(stock_rows(m, line(), sticker), [{"row_no": 1, "qty": 4}])
    assert len(selected) == 1 and selected[0]["qty"] == 4


def test_all_includes_stickers_but_excludes_draft_quantities(harness):
    m, *_ = harness
    sticker = line("sticker", 2)
    sticker.update(rate=0, is_free_item=1)
    rows = stock_rows(m, line(), sticker, drafts=[{"so_detail": "row-a", "qty": 3}])
    selected = m.select_rows(rows, all_remaining=True)
    assert [r["qty"] for r in selected] == [7, 10]
    assert selected[1]["is_free_item"]


def test_returns_are_not_subtracted_twice(harness):
    m, *_ = harness
    r = line()
    r.update(qty=10, delivered_qty=6, returned_qty=2)
    assert stock_rows(m, r)[0]["available_qty"] == 4


def test_same_sku_keeps_separate_order_rows(harness):
    m, *_ = harness
    rows = stock_rows(m, line(), line("row-b", 2))
    result = m.select_rows(rows, [{"row_no": 2, "qty": 3}])
    assert result[0]["sales_order_item"] == "row-b"
    with pytest.raises(m.DeliveryInputError):
        m.select_rows(rows, [{"item_code": "SAME-SKU", "qty": 3}])


@pytest.mark.parametrize("qty", [0, -1, 11, True, "NaN", "Infinity", None, 1.5])
def test_invalid_quantities_cannot_create_delivery(harness, qty):
    m, *_ = harness
    with pytest.raises(m.DeliveryInputError):
        m.select_rows(stock_rows(m), [{"row_no": 1, "qty": qty}])


def test_fractional_sales_unit_cannot_create_fractional_stock_unit(harness):
    m, *_ = harness
    r = line()
    r.update(uom="Carton", conversion_factor=3)
    rows = stock_rows(m, r)
    with pytest.raises(m.DeliveryInputError, match="整数"):
        m.select_rows(rows, [{"row_no": 1, "qty": 0.5}])
    assert m.select_rows(rows, [{"row_no": 1, "qty": 2}])[0]["stock_qty"] == 6


def test_duplicate_or_mismatched_row_identifier_rejected(harness):
    m, *_ = harness
    rows = stock_rows(m)
    with pytest.raises(m.DeliveryInputError, match="重复"):
        m.select_rows(rows, [{"row_no": 1, "qty": 2}, {"sales_order_item": "row-a", "qty": 1}])
    with pytest.raises(m.DeliveryInputError):
        m.select_rows(rows, [{"sales_order_item": "row-a", "row_no": 2, "qty": 1}])


def test_occupied_and_direct_delivery_rows_not_available(harness):
    m, *_ = harness
    r = line()
    r["delivered_by_supplier"] = 1
    assert stock_rows(m, r) == []
    with pytest.raises(m.DeliveryInputError, match="草稿占用"):
        m.select_rows(stock_rows(m, drafts=[{"so_detail": "row-a", "qty": 20}]), all_remaining=True)


@pytest.mark.parametrize("paid,status,eligible,remaining", [(0,"unpaid",False,100),(40,"partial",True,60),(100,"paid",True,0),(120,"paid",True,0)])
def test_payment_flexibility(harness, paid, status, eligible, remaining):
    m, *_ = harness
    result = m.payment_status(100, paid, "USD")
    assert (result["status"], result["eligible"], result["remaining"]) == (status, eligible, remaining)


def test_free_order_needs_no_fake_receipt(harness):
    m, *_ = harness
    assert m.payment_status(0, 0, "USD")["status"] == "not_required"


def test_cancelled_receipt_and_noncash_writeoff_are_not_paid(harness):
    m, _, s, order = harness
    s.entries = [receipt(voucher="CANCELLED"), receipt("GL-2", "WRITE-OFF", 100, kind="Journal Entry")]
    s.cancelled.add("CANCELLED")
    s.noncash.add("WRITE-OFF")
    assert m.get_payment_summary(order)["status"] == "unpaid"
    order.check_permission.assert_called_once_with("read")


def test_refund_offsets_receipt_instead_of_absolute_value(harness):
    m, _, s, order = harness
    s.entries = [receipt(amount=100), receipt("REFUND", "PE-REFUND", -100)]
    assert m.get_payment_summary(order)["status"] == "unpaid"


def test_same_gl_receipt_counted_once_even_with_order_and_invoice_links(harness):
    m, _, s, order = harness
    s.invoices = ["INV-1"]
    row = receipt(amount=100, advance_voucher_type="Sales Order", advance_voucher_no="SO-1")
    s.entries = [row, row]
    assert m.get_payment_summary(order)["paid"] == 100


def test_paypal_gross_receipt_not_net_bank_after_fee(harness):
    m, _, s, order = harness
    s.entries = [receipt(voucher="PAYPAL-JV", amount=100, kind="Journal Entry")]
    assert m.get_payment_summary(order)["paid"] == 100


def test_mixed_order_invoice_cannot_claim_all_its_receipts(harness):
    m, _, s, order = harness
    s.invoices, s.mixed, s.uncertain = ["MIXED-INV"], True, True
    with pytest.raises(m.DeliveryInputError, match="收款归属"):
        m.get_payment_summary(order)


def test_unreadable_order_does_not_query_receipt_records(harness):
    m, _, s, order = harness
    order.check_permission.side_effect = PermissionError("no read")
    with pytest.raises(PermissionError):
        m.get_payment_summary(order)
    assert s.queries == []


def test_unknown_receipt_currency_stops_without_guessing(harness):
    m, _, s, order = harness
    s.entries = [receipt(currency="EUR")]
    with pytest.raises(m.DeliveryInputError, match="币种"):
        m.get_payment_summary(order)


def test_current_receipt_read_is_locked_during_creation(harness):
    m, _, s, order = harness
    s.entries = [receipt()]
    m.get_payment_summary(order, for_update=True)
    assert all(query.rstrip().endswith("for update") for query, _ in s.queries)


@pytest.mark.parametrize("advance,target", [("OTHER-SO", "INV-1"), ("SO-1", "OTHER-INV")])
def test_advance_transferred_across_orders_needs_allocation_review(harness, advance, target):
    m, _, s, order = harness
    s.invoices = ["INV-1"]
    s.entries = [receipt(advance_voucher_type="Sales Order", advance_voucher_no=advance,
                         against_voucher_type="Sales Invoice", against_voucher=target)]
    with pytest.raises(m.DeliveryInputError, match="跨订单"):
        m.get_payment_summary(order)
