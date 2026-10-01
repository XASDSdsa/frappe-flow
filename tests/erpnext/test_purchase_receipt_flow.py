"""Business rules only: no simulated Frappe save/submit or fake posting claims."""
import ast
from collections import defaultdict
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import math

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "flow/integrations/erpnext/purchase_receipt_flow.py"


class InputError(Exception):
    def __init__(self, message, fields=None):
        super().__init__(message)
        self.fields = fields or []


class Row(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__


@pytest.fixture
def rules():
    """Load pure business functions, without pretending to run native controllers."""
    tree = ast.parse(SOURCE.read_text())
    functions = {"_number", "_cost", "_text", "_request", "_available_rows", "_select", "_read", "_order_state", "_verify"}
    constants = {"REQUEST_FIELDS", "EXISTING_FIELDS", "DEFAULT_WAREHOUSE"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in functions or
             isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets)]
    fake = SimpleNamespace(get_doc=Mock(), db=SimpleNamespace(sql=Mock(return_value=[])))
    namespace = {"InputError": InputError, "math": math, "date": date,
                 "defaultdict": defaultdict, "nowdate": lambda: "2026-09-18", "frappe": fake}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return SimpleNamespace(**namespace)


def source(**overrides):
    return Row({"name": "POI-1", "idx": 1, "item_code": "STICKER-C1", "item_name": "客户贴纸", "qty": 1000,
        "received_qty": 250, "uom": "张", "warehouse": "大坪仓库 - LEYA", "rate": 0.6, **overrides})


def available(rules, drafts=None):
    return rules._available_rows(Row(items=[source()]), drafts or [])


def direct_request(**updates):
    return {"supplier": "SUP-1", "items": [{"item_code": "STICKER-C1", "qty": 1000, "rate": 0.6}], **updates}


@pytest.mark.parametrize("value", [None, "", True, False, 0, -1, float("nan"), float("inf"), "Infinity", {}])
def test_actual_cost_and_quantity_must_be_positive_finite(rules, value):
    with pytest.raises(InputError):
        rules._number(value, "实际成本")


def test_direct_receipt_collects_supplier_quantity_and_cost_at_once(rules):
    with pytest.raises(InputError) as error:
        rules._request({"items": [{"item_code": "STICKER-C1"}, {"item_code": "P2", "qty": True, "rate": 0}]})
    assert error.value.fields == ["supplier", "items[1].qty", "items[1].rate", "items[2].qty", "items[2].rate"]


def test_source_cost_may_be_native_but_quantity_is_never_assumed(rules):
    request = {"purchase_order": "PO-1", "items": [{"purchase_order_item": "POI-1", "qty": 100}]}
    assert rules._request(request)["submit"] is False
    request["items"][0].pop("qty")
    with pytest.raises(InputError) as error:
        rules._request(request)
    assert error.value.fields == ["items[1].qty"]


def test_default_is_draft_and_receipt_date_is_frozen(rules):
    result = rules._request(direct_request())
    assert result["submit"] is False
    assert result["posting_date"] == "2026-09-18"


def test_submit_requires_explicit_actual_arrival(rules):
    with pytest.raises(InputError, match="实际已到货"):
        rules._request(direct_request(submit=True))
    assert rules._request(direct_request(submit=True, actual_receipt=True))["submit"]


@pytest.mark.parametrize("extra", [{"ignore_permissions": True}, {"allow_zero_valuation_rate": True}, {"submit": "true"}])
def test_unknown_or_coerced_controls_are_rejected(rules, extra):
    with pytest.raises(InputError):
        rules._request(direct_request(**extra))


def test_unknown_item_extras_are_not_silently_discarded(rules):
    request = direct_request()
    request["items"][0]["allow_zero_valuation_rate"] = 1
    with pytest.raises(InputError) as error:
        rules._request(request)
    assert "items[1].allow_zero_valuation_rate" in error.value.fields


@pytest.mark.parametrize("override", [{"items": []}, {"warehouse": "OTHER"}, {"supplier": "SUP2"}, {"posting_date": "2026-09-17"}])
def test_existing_draft_cannot_hide_overrides(rules, override):
    with pytest.raises(InputError, match="隐藏覆盖"):
        rules._request({"existing_document": "PR-1", **override})
    assert rules._request({"existing_document": "PR-1", "submit": True, "actual_receipt": True})["submit"]


def test_actual_receipt_date_cannot_be_future_or_ambiguous(rules):
    for value in ("2026-09-19", "20260918", "18/09/2026", "2026-09-18T10:00:00"):
        with pytest.raises(InputError):
            rules._request(direct_request(posting_date=value))


def test_available_subtracts_actual_received_and_all_active_drafts(rules):
    drafts = [Row(purchase_order_item="POI-1", item_code="STICKER-C1", uom="张", qty=100, received_qty=100),
              Row(purchase_order_item="POI-1", item_code="STICKER-C1", uom="张", qty=40, received_qty=50, rejected_qty=10)]
    row = available(rules, drafts)[0]
    assert row["available_qty"] == 600
    assert row["received_qty"] == 250
    assert row["draft_qty"] == 150


def test_stale_source_cannot_receive_more_than_current_remaining(rules):
    selection = [{"purchase_order_item": "POI-1", "qty": 700}]
    assert rules._select(available(rules), selection)[0]["qty"] == 700
    draft = Row(purchase_order_item="POI-1", item_code="STICKER-C1", uom="张", qty=100, received_qty=100)
    with pytest.raises(InputError, match="最多可收 650"):
        rules._select(available(rules, [draft]), selection)


def test_returns_are_not_subtracted_twice(rules):
    row = source(received_qty=200, returned_qty=50)
    assert rules._available_rows(Row(items=[row]), [])[0]["available_qty"] == 800


def test_exact_source_row_not_sku_controls_selection(rules):
    row2 = source(name="POI-2", idx=2, rate=0.8)
    rows = rules._available_rows(Row(items=[source(), row2]), [])
    assert rules._select(rows, [{"purchase_order_item": "POI-2", "qty": 20}])[0]["rate"] == 0.8
    for bad in ({"purchase_order_item": "STICKER-C1", "qty": 20},
                {"purchase_order_item": "POI-1", "qty": 20, "item_code": "OTHER"},
                {"purchase_order_item": "POI-1", "qty": 20, "uom": "箱"}):
        with pytest.raises(InputError):
            rules._select(rows, [bad])


def test_duplicate_source_rows_and_direct_items_are_rejected(rules):
    choice = {"purchase_order_item": "POI-1", "qty": 10}
    with pytest.raises(InputError, match="不能重复"):
        rules._select(available(rules), [choice, choice])
    request = direct_request()
    request["items"] *= 2
    with pytest.raises(InputError, match="不可重复"):
        rules._request(request)


def test_zero_source_price_requires_explicit_actual_cost(rules):
    rows = rules._available_rows(Row(items=[source(rate=0)]), [])
    with pytest.raises(InputError, match="零成本"):
        rules._select(rows, [{"purchase_order_item": "POI-1", "qty": 100}])
    assert rules._select(rows, [{"purchase_order_item": "POI-1", "qty": 100, "rate": 0.6}])[0]["rate"] == 0.6


def test_explicit_zero_cost_reason_allows_direct_and_purchase_order_receipts(rules):
    reason = "贴纸服务成本已另行入账，本次仅入库数量"
    request = direct_request(items=[{"item_code": "STICKER-C1", "qty": 1000, "rate": 0}],
                             zero_valuation_reason=reason, submit=True, actual_receipt=True)
    assert rules._request(request)["items"][0]["rate"] == 0
    rows = rules._available_rows(Row(items=[source(rate=0)]), [])
    assert rules._select(rows, [{"purchase_order_item": "POI-1", "qty": 100}], reason)[0]["rate"] == 0
    for invalid in (None, "", "   ", True):
        with pytest.raises(InputError):
            rules._request({**request, "zero_valuation_reason": invalid})
    request["items"][0]["qty"] = 0
    with pytest.raises(InputError):
        rules._request(request)


@pytest.mark.parametrize("rate,value,gl,reason,allowed", [
    (0, 0, [], "服务成本已另行入账", True),
    (0, 10, [], "服务成本已另行入账", False),
    (0, 0, [], "", False),
    (0, 0, [Row(company="C", debit=600, credit=0), Row(company="C", debit=0, credit=600)], "服务成本已另行入账", False),
    (0.6, 600, [], "", False),
    (0.6, 600, [Row(company="C", debit=600, credit=0), Row(company="C", debit=0, credit=600)], "", True),
])
def test_posting_verification_accepts_zero_value_without_gl_but_checks_normal_cost(rules, monkeypatch, rate, value, gl, reason, allowed):
    import sys
    import types
    erpnext = types.ModuleType("erpnext")
    erpnext.is_perpetual_inventory_enabled = lambda _company: True
    utils = types.ModuleType("frappe.utils")
    utils.flt = lambda value: float(value or 0)
    monkeypatch.setitem(sys.modules, "erpnext", erpnext)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    row = Row(name="ROW", item_code="STICKER", warehouse="WH", stock_qty=1000,
              rate=rate, allow_zero_valuation_rate=int(rate == 0))
    doc = SimpleNamespace(name="PR", company="C", grand_total=rate*1000, items=[row], get=lambda key: [])
    rules.frappe.get_cached_value = lambda *_args: True
    rules.frappe.get_all = lambda dt, **_args: ([Row(company="C", item_code="STICKER", warehouse="WH",
                                                   actual_qty=1000, stock_value_difference=value)] if dt == "Stock Ledger Entry" else gl)
    request = {"submit": True, "zero_valuation_reason": reason}
    if allowed:
        rules._verify(doc, request)
    else:
        with pytest.raises(InputError):
            rules._verify(doc, request)


def test_corrupt_draft_source_fails_closed(rules):
    with pytest.raises(InputError, match="单位不一致"):
        available(rules, [Row(purchase_order_item="POI-1", item_code="STICKER-C1", uom="箱", qty=2)])


def test_read_permission_is_checked_without_elevation(rules):
    doc = Mock()
    doc.check_permission.side_effect = PermissionError("denied")
    rules.frappe.get_doc.return_value = doc
    with pytest.raises(PermissionError):
        rules._read("Purchase Order", "PO-1", for_update=True)
    rules.frappe.get_doc.assert_called_once_with("Purchase Order", "PO-1", for_update=True)
    doc.check_permission.assert_called_once_with("read")


def test_execution_locks_source_and_both_types_of_stock_receipt_drafts(rules):
    order = Row(name="PO-1", docstatus=1, status="To Receive and Bill", items=[source()], check_permission=Mock())
    rules.frappe.get_doc.return_value = order
    _, rows = rules._order_state("PO-1", for_update=True, exclude_receipt="PR-SELF")
    rules.frappe.get_doc.assert_called_once_with("Purchase Order", "PO-1", for_update=True)
    queries = rules.frappe.db.sql.call_args_list
    assert len(queries) == 2 and all(call.args[0].endswith(" for update") for call in queries)
    assert queries[0].args[1] == ("PO-1", "PR-SELF")
    assert "d.update_stock=1" in queries[1].args[0]
    assert rows[0]["available_qty"] == 750
