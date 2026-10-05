"""Focused integration checks between reviewed pricing and native packing rows."""
from copy import deepcopy
import sys
import types
from unittest.mock import Mock

import pytest

from test_sales_order_sticker_review import flow, native_order, native_row


class NativeDoc(types.SimpleNamespace):
    def get(self, field):
        return getattr(self, field, None)

    def set(self, field, value):
        setattr(self, field, value)


@pytest.fixture
def bundled(flow, monkeypatch):
    module, harness = flow
    harness.frappe.get_cached_doc = lambda dt: {"auto_insert_price_list_rate_if_missing": 1, "update_existing_price_list_rate": 1}
    harness.sticker()
    resolved = harness.call(include_stickers=True)
    assert resolved["status"] == "ready", resolved
    rows = [native_row(None, "GREEN", 120, 3), native_row(None, "STICKER", 120, 0, True)]
    doc = NativeDoc(**vars(native_order(rows)), packed_items=[])
    doc.total = doc.net_total = doc.grand_total = doc.rounded_total = 360
    m = types.ModuleType("flow.integrations.erpnext.sales_order_bundles")
    bundles = {}
    def ensure(product, sticker, customer, **kwargs):
        value = {"item_code": "BUNDLE-" + product, "item_name": "山东绿方＋客户绿方贴纸",
                 "disposition": "create", "signature": "same-" + product,
                 "components": [{"item_code": product, "item_name": product, "qty": 1, "uom": "Nos"},
                                {"item_code": sticker, "item_name": "客户绿方贴纸", "qty": 1, "uom": "Nos"}]}
        bundles[value["item_code"]] = value
        return deepcopy(value)
    m.ensure_bundle = Mock(side_effect=ensure)
    m.bundle_code = lambda customer, product, sticker: "BUNDLE-" + product
    monkeypatch.setitem(sys.modules, m.__name__, m)
    state = types.SimpleNamespace(module=module, resolved=resolved, doc=doc, bundles=m,
                                  mutate=None, harness=harness)
    def native_validate(method):
        if method != "validate":
            return
        doc.packed_items = []
        for parent in doc.items:
            for component in bundles.get(parent.item_code, {}).get("components", []):
                doc.packed_items.append(types.SimpleNamespace(
                    parent_item=parent.item_code, parent_detail_docname=parent.name,
                    item_code=component["item_code"], uom=component["uom"],
                    qty=parent.stock_qty, warehouse=parent.warehouse))
        if state.mutate:
            state.mutate(doc)
    doc.run_method = native_validate
    module._build_order = Mock(return_value=doc)
    return state


def test_no_pairing_keeps_original_native_order_and_rows(bundled):
    h = bundled
    h.resolved["sticker_rows"] = []
    original = h.doc.items
    doc, resolved, bundles = h.module._build_reviewed_order(h.resolved, {})
    assert doc is h.doc and doc.items is original and resolved is h.resolved
    assert bundles == []
    h.bundles.ensure_bundle.assert_not_called()


def test_pairing_replaces_rows_preserves_native_price_and_component_stock_quantities(bundled):
    h = bundled
    h.doc.items[0].qty, h.doc.items[0].uom, h.doc.items[0].rate = 6, "Box", 60
    touched = set()
    doc, resolved, bundles = h.module._build_reviewed_order(h.resolved, {}, touched=touched)
    assert len(doc.items) == 1 and doc.items[0].item_code == "BUNDLE-GREEN"
    assert (doc.items[0].qty, doc.items[0].rate, doc.items[0].amount, doc.grand_total) == (6, 60, 360, 360)
    assert [r.qty for r in doc.packed_items] == [120, 120]
    assert resolved["items"][0]["row_type"] == "bundle"
    assert resolved["items"][0]["product_item_code"] == "GREEN"
    assert resolved["items"][0]["sticker_item_code"] == "STICKER"
    assert touched == {"BUNDLE-GREEN"}
    assert bundles[0]["product_row"] == 1
    assert h.resolved["items"][0]["item_code"] == "GREEN"


@pytest.mark.parametrize("field,value", [("item_code", "WRONG-STICKER"), ("qty", 119),
                                         ("warehouse", "OTHER"), ("parent_item", "WRONG-BUNDLE"),
                                         ("parent_detail_docname", "OTHER-ROW")])
def test_packed_identity_quantity_and_warehouse_must_match(bundled, field, value):
    h = bundled
    h.mutate = lambda doc: setattr(doc.packed_items[1], field, value)
    with pytest.raises(h.module.OrderInputError, match="不一致"):
        h.module._build_reviewed_order(h.resolved, {})


def test_changed_bundle_master_signature_requires_new_review(bundled):
    h = bundled
    with pytest.raises(h.module.OrderInputError, match="组合配置已变化"):
        h.module._build_reviewed_order(h.resolved, {}, planned_bundles=[
            {"product_row": 1, "item_code": "BUNDLE-GREEN", "signature": "previous-master"}])


@pytest.mark.parametrize("change,reason", [("rate", "定价改变"), ("tax", "金额或税费")])
def test_native_repricing_or_tax_changes_are_rejected(bundled, change, reason):
    h = bundled
    h.mutate = (lambda doc: setattr(doc.items[0], "rate", 9)) if change == "rate" else (
        lambda doc: setattr(doc, "grand_total", 370))
    with pytest.raises(h.module.OrderInputError, match=reason):
        h.module._build_reviewed_order(h.resolved, {})


def test_approval_card_shows_exact_sticker_pairing_zero_price_and_stock_behavior(bundled):
    h = bundled
    doc, resolved, bundles = h.module._build_reviewed_order(h.resolved, {})
    summary = h.module._summary(doc, resolved)
    h.module._load_plan = lambda token: {"summary": summary, "bundles": bundles}
    card = h.module._confirmation_prompt({"preview_token": "review"})
    assert "【组合商品：巧克粉＋客户贴纸】" in card
    assert "山东绿方＋客户绿方贴纸" in card
    assert "贴 Customer sticker STICKER，120张，贴纸0元" in card
    assert "批准后新建组合" in card and "出库分别扣巧克粉与贴纸库存" in card
    assert summary["product_amount"] == 360 and summary["sticker_amount"] == 0
    assert summary["product_qty"] == summary["sticker_qty"] == 120


def test_multiple_new_bundle_rows_have_distinct_native_parent_links(bundled):
    h = bundled
    product, sticker = deepcopy(h.resolved["items"][0]), deepcopy(h.resolved["sticker_rows"][0])
    product.update(item_code="BLUE", product_row=2)
    sticker.update(item_code="BLUE-STICKER", product_row=2)
    h.resolved["items"].extend([product, sticker])
    h.resolved["sticker_rows"].append(sticker)
    h.doc.items.extend([native_row(None, "BLUE", 120, 3), native_row(None, "BLUE-STICKER", 120, 0, True)])
    doc, _, _ = h.module._build_reviewed_order(h.resolved, {})
    assert len({row.name for row in doc.items}) == 2
    for parent in doc.items:
        components = [row for row in doc.packed_items if row.parent_detail_docname == parent.name]
        assert len(components) == 2 and all(row.parent_item == parent.item_code for row in components)


def test_orphan_packed_component_cannot_hide_outside_verified_bundle(bundled):
    h = bundled
    h.mutate = lambda doc: doc.packed_items.append(types.SimpleNamespace(
        parent_detail_docname="unrelated", parent_item="BUNDLE-GREEN", item_code="HIDDEN-STICKER",
        qty=120, uom="Nos", warehouse="WH"))
    with pytest.raises(h.module.OrderInputError):
        h.module._build_reviewed_order(h.resolved, {})
