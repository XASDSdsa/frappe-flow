"""Standalone sticker pricing/review and saved-order classification boundaries."""

import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest

from test_sales_order_resolution import Harness


@pytest.fixture
def flow(monkeypatch):
    harness = Harness(monkeypatch)
    utils = sys.modules["frappe.utils"]
    utils.now_datetime = Mock()
    utils.strip_html = lambda value: value
    utils.get_system_timezone = lambda: "Asia/Shanghai"
    harness.frappe.utils = utils
    monkeypatch.setitem(sys.modules, "flow.integrations.erpnext.sales_order_resolution", harness.module)
    tool_module = types.ModuleType("flow.lib.tool")
    tool_module.tool = lambda function, **_options: function
    monkeypatch.setitem(sys.modules, "flow.lib.tool", tool_module)
    path = Path(__file__).resolve().parents[2] / "flow/integrations/erpnext/sales_order_flow.py"
    spec = importlib.util.spec_from_file_location("flow.integrations.erpnext.sales_order_flow", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, harness


def native_row(name, code, qty, rate, free=False):
    return types.SimpleNamespace(name=name, item_code=code, item_name=code, qty=qty, uom="Nos", stock_qty=qty,
                                 stock_uom="Nos", rate=rate, amount=qty * rate, warehouse="WH",
                                 delivery_date="2026-09-25", is_free_item=free)


def native_order(rows):
    return types.SimpleNamespace(
        items=rows, customer="CUSTOMER", customer_name="Exact Customer", company="COMPANY", currency="USD",
        selling_price_list="Selling", conversion_rate=7, price_list_currency="USD", plc_conversion_rate=1,
        transaction_date="2026-09-18", delivery_date="2026-09-25",
        sales_team=[types.SimpleNamespace(sales_person="Alice", allocated_percentage=100)],
        total=460, net_total=460, taxes=[], total_taxes_and_charges=0, grand_total=460,
        rounded_total=460, disable_rounded_total=True, customer_address="ADDRESS",
        shipping_address_name="SHIPPING", shipping_address="Full shipping address", contact_person="CONTACT",
        payment_terms_template=None, terms="",
    )


def resolved_rows():
    return [{"item_code": "STICKER", "row_type": "standalone_sticker", "sticker_model": "Green", "sticker_version": "v1"},
            {"item_code": "GREEN", "row_type": "product"},
            {"item_code": "STICKER", "row_type": "sticker", "product_item_code": "GREEN",
             "sticker_model": "Green", "sticker_version": "v1"}]


@pytest.mark.parametrize("row_type, expected", [("product", "本单未列贴纸"), ("bundle", "本单已选择贴纸"),
                                               ("standalone_sticker", "本单已选择贴纸")])
def test_first_order_sticker_choice_appears_in_overall_approval(flow, row_type, expected):
    module, harness = flow
    harness.frappe.get_list = Mock(return_value=[])
    summary = module._summary(native_order([native_row("row", "GREEN", 120, 3)]),
                              {"items": [{"item_code": "GREEN", "row_type": row_type}]})
    fingerprint = module._hash(summary)
    review = module._first_order_sticker_review(summary)
    assert review["display_mode"] == "review_notice"
    assert review["requires_input"] is False
    harness.frappe.get_list.assert_called_once_with(
        "Sales Order", filters={"customer": "CUSTOMER", "docstatus": ["!=", 2]},
        fields=["name"], limit_page_length=1)
    module._load_plan = lambda _token: {"summary": summary, "sticker_service_review": review}
    card = module._confirmation_prompt({"preview_token": "trusted-token"})
    assert "⚠️ **首单贴纸提醒**" in card
    assert expected in card and "不增加第二次确认" in card
    assert module._hash(summary) == fingerprint


def test_existing_order_suppresses_first_order_reminder_and_permission_failure_is_explicit(flow):
    module, harness = flow
    summary = {"customer": "CUSTOMER", "items": []}
    harness.frappe.get_list = Mock(return_value=[{"name": "EXISTING-DRAFT"}])
    assert module._first_order_sticker_review(summary) is None
    harness.frappe.get_list.side_effect = PermissionError("restricted")
    assert "无法确认是否首单" in module._first_order_sticker_review(summary)["message"]


def test_approval_card_separates_both_sticker_kinds_and_shows_warning_from_trusted_summary(flow):
    module, _ = flow
    order = native_order([native_row("independent", "STICKER", 1000, .1),
                          native_row("product", "GREEN", 120, 3), native_row("companion", "STICKER", 120, 0, True)])
    summary = module._summary(order, {"items": resolved_rows()})
    assert summary["product_qty"] == 120
    assert summary["sticker_qty"] == 1120
    assert summary["accompanying_sticker_qty"] == 120
    assert summary["standalone_sticker_qty"] == 1000
    assert summary["product_amount"] == 360
    assert summary["sticker_amount"] == summary["standalone_sticker_amount"] == 100
    assert summary["accompanying_sticker_amount"] == 0
    module._load_plan = lambda _token: {"summary": summary}
    card = module._confirmation_prompt({"preview_token": "trusted-token"})
    assert card.index("⚠️ **重点审核：独立销售贴纸**") < card.index("【商品】")
    assert "【配套贴纸】" in card and "【独立销售贴纸】" in card
    assert "STICKER：1000张；单价 USD 0.1，小计 100.00；型号版本 Green/v1" in card
    assert "STICKER：120张；免费，金额0.00；型号版本 Green/v1；对应商品 GREEN" in card
    assert "无需另行确认" in card


def test_existing_order_reordered_rows_use_persisted_child_identity_not_position(flow):
    module, _ = flow
    original_rows = resolved_rows()
    row_sources = dict(zip(["independent", "product", "companion"], original_rows))
    order = native_order([native_row("companion", "STICKER", 120, 0, True),
                          native_row("independent", "STICKER", 1000, .1), native_row("product", "GREEN", 120, 3)])
    summary = module._summary(order, {"items": original_rows}, existing=True, row_sources=row_sources)
    assert [row["row_type"] for row in summary["items"]] == ["sticker", "standalone_sticker", "product"]
    assert summary["standalone_sticker_amount"] == 100
    assert summary["product_amount"] == 360
    order.items[0].item_code = "MANUALLY-REPLACED"
    assert module._summary(order, {"items": original_rows}, existing=True, row_sources=row_sources)["items"][0]["row_type"] == "native"


def test_legacy_reuse_matches_unique_item_classification_and_does_not_guess_ambiguous_stickers(flow):
    module, _ = flow
    order = native_order([native_row("product", "GREEN", 120, 3), native_row("sticker", "STICKER", 120, 0, True)])
    summary = module._summary(order, {"items": resolved_rows()}, existing=True)
    assert [row["row_type"] for row in summary["items"]] == ["product", "native"]
    unique = {"items": resolved_rows()[1:]}
    order.items.reverse()
    summary = module._summary(order, unique, existing=True)
    assert [row["row_type"] for row in summary["items"]] == ["sticker", "product"]


def test_standalone_sticker_without_price_uses_zero_sale_price_but_stays_stock_item(flow, monkeypatch):
    module, harness = flow
    harness.sticker()
    resolved = harness.call(items=[{"item_code": "STICKER", "qty": 1000}])
    assert resolved["status"] == "ready"
    row = native_row("unpriced", "STICKER", 1000, 0)
    row.delivered_by_supplier = False
    doc = native_order([])
    doc.update = lambda values: doc.__dict__.update(values)
    doc.set = lambda field, value: setattr(doc, field, value)
    doc.append = lambda _field, values: (row.__dict__.update(values), doc.items.append(row), row)[-1]
    doc.check_permission = Mock()
    doc.set_missing_values = Mock()
    doc.calculate_taxes_and_totals = Mock()
    harness.frappe.new_doc = lambda _doctype: doc
    harness.frappe.get_cached_doc = lambda _doctype: {"auto_insert_price_list_rate_if_missing": 1,
                                                   "update_existing_price_list_rate": 1}
    harness.frappe.get_cached_value = lambda *_: 1
    module._company = lambda _company: "COMPANY"
    module._read = lambda doctype, name: (
        types.SimpleNamespace(enabled=1, selling=1, currency="USD") if doctype == "Price List"
        else types.SimpleNamespace(company="COMPANY", is_group=0, disabled=0)
    )
    party = types.ModuleType("erpnext.accounts.party")
    party.get_default_price_list = lambda _customer: "Selling"
    monkeypatch.setitem(sys.modules, "erpnext.accounts.party", party)
    doc.name = None
    doc.run_method = Mock()
    doc.set_parent_in_children = Mock()
    doc._validate = Mock()
    result = module._build_order(resolved, {"base_date": "2026-09-18", "price_list": "Selling"})
    assert result.items[0].rate == result.items[0].price_list_rate == 0
    assert result.items[0].is_free_item == 1
    assert result.items[0].warehouse == module.DEFAULT_WAREHOUSE
