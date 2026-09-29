"""Conversation boundary, one-approval draft creation and transaction behavior."""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "flow.integrations.erpnext."


def load_module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def flow(monkeypatch):
    h = types.SimpleNamespace(user="sales@example.com", scope="session-1", message="user-1", cache={}, ledgers={}, saved={}, readback_error=False)
    f = types.ModuleType("frappe")
    f.local = types.SimpleNamespace(site="test.local")
    f.PermissionError = PermissionError
    f.cache = types.SimpleNamespace(get_value=h.cache.get, set_value=lambda k, v, **kw: h.cache.__setitem__(k, v))
    f.db = types.SimpleNamespace(savepoint=Mock(), rollback=Mock(), get_value=lambda *a, **kw: h.message,
        sql=lambda query, name: [(name,)] if name in h.ledgers else [])
    util = types.ModuleType("frappe.utils")
    util.nowdate = lambda: "2026-09-17"
    util.now_datetime = lambda: datetime(2026, 9, 17, 12)
    util.get_datetime = datetime.fromisoformat
    util.strip_html = str
    f.utils = util
    monkeypatch.setitem(sys.modules, "frappe", f)
    monkeypatch.setitem(sys.modules, "frappe.utils", util)
    rules = load_module(PACKAGE + "delivery_note_rules", "flow/integrations/erpnext/delivery_note_rules.py")
    monkeypatch.setitem(sys.modules, PACKAGE + "delivery_note_rules", rules)
    helpers = types.ModuleType(PACKAGE + "sales_order_flow")
    helpers._actor = lambda: h.user
    helpers._scope = lambda: h.scope
    helpers._json = lambda v: json.dumps(v, sort_keys=True, default=str)
    helpers._hash = lambda v: hashlib.sha256(helpers._json(v).encode()).hexdigest()
    @contextmanager
    def without_prices():
        yield
    helpers._without_price_maintenance = without_prices
    monkeypatch.setitem(sys.modules, PACKAGE + "sales_order_flow", helpers)
    tool_module = types.ModuleType("flow.lib.tool")
    def tool(fn, **kwargs):
        fn.__dict__.update(kwargs)
        return fn
    tool_module.tool = tool
    monkeypatch.setitem(sys.modules, "flow.lib.tool", tool_module)
    m = load_module(PACKAGE + "delivery_note_flow_test", "flow/integrations/erpnext/delivery_note_flow.py")
    row = {"row_no": 1, "sales_order_item": "row-1", "item_code": "PRODUCT", "item_name": "商品", "available_qty": 10,
           "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1, "warehouse": "大坪仓库 - LEYA", "rate": 3,
           "is_free_item": False, "whole_uom": True, "whole_stock_uom": True}
    h.state = {"sales_order": "SO-1", "customer": "C-1", "customer_name": "客户", "company": "Co", "currency": "USD",
               "order_modified": "2026-09-17 10:00", "items": [row], "drafts": [], "draft_count": 0, "order_status": "To Deliver",
               "payment": {"status": "partial", "eligible": True, "currency": "USD", "order_total": 30,
                           "paid": 15, "remaining": 15, "message": "部分收款，请核对未收余额。"}}
    h.order = types.SimpleNamespace(name="SO-1")
    m._order_state = Mock(side_effect=lambda *a, **kw: (h.order, json.loads(json.dumps(h.state))))
    h.doc = types.SimpleNamespace(name=None, owner=h.user, docstatus=0, check_permission=Mock(), submit=Mock())
    h.summary = {"customer": "C-1", "customer_name": "客户", "company": "Co", "currency": "USD", "posting_date": "2026-09-17",
                 "shipping_address_display": "收货地址", "grand_total": 12, "packed_items": [],
                 "items": [{**row, "qty": 4, "stock_qty": 4}]}
    m._document_summary = Mock(side_effect=lambda doc: json.loads(json.dumps(h.summary)))
    h.build = m._build_delivery = Mock(return_value=h.doc)
    def insert():
        h.doc.name = "DN-1"
        h.saved[h.doc.name] = h.doc
    h.doc.insert = Mock(side_effect=insert)
    def get_doc(doctype, name=None, **kwargs):
        if isinstance(doctype, dict):
            doc = types.SimpleNamespace(**doctype, flags=types.SimpleNamespace())
            def insert_ledger(**kwargs):
                doc.name = doc.flags._name
                h.ledgers[doc.name] = doc
            doc.insert = Mock(side_effect=insert_ledger)
            doc.save = Mock()
            return doc
        if doctype == "Integration Request":
            return h.ledgers[name]
        if doctype == "Delivery Note":
            if h.readback_error:
                raise ValueError("模拟回读失败")
            return h.saved[name]
        raise AssertionError(doctype)
    f.get_doc = get_doc
    h.module, h.frappe = m, f
    return h


def prepare(h):
    m = h.module
    options = m.get_delivery_note_options("SO-1")
    assert options["status"] == "needs_selection"
    h.message = "user-2"
    preview = m.preview_delivery_note(options["selection_token"], [{"row_no": 1, "qty": 4}])
    assert preview["status"] == "preview", preview
    return preview["preview_token"]


def test_must_wait_for_customer_reply_after_listing(flow):
    h, m = flow, flow.module
    options = m.get_delivery_note_options("SO-1")
    result = m.preview_delivery_note(options["selection_token"], all_remaining=True)
    assert result["status"] == "needs_input" and "尚未回复选择" in result["reason"]
    h.build.assert_not_called()


def test_reply_without_explicit_selection_does_not_default_to_all(flow):
    h, m = flow, flow.module
    options = m.get_delivery_note_options("SO-1")
    h.message = "user-2"
    result = m.preview_delivery_note(options["selection_token"])
    assert result["status"] == "needs_input" and result["missing"] == ["items"]
    h.build.assert_not_called()


def test_preview_has_no_business_writes_and_rolls_back_native_side_effects(flow):
    token = prepare(flow)
    assert token and not flow.ledgers and not flow.saved
    flow.doc.insert.assert_not_called()
    assert flow.frappe.db.rollback.call_args.kwargs["save_point"].startswith("delivery_preview_")


def test_unpaid_order_displays_reason_and_has_no_selection_token(flow):
    flow.state["payment"].update(status="unpaid", eligible=False, paid=0, remaining=30, message="尚未查到有效收款")
    result = flow.module.get_delivery_note_options("SO-1")
    assert result["status"] == "blocked" and result["selection_token"] is None


@pytest.mark.parametrize("field,value", [("user", "another@example.com"), ("scope", "another-session")])
def test_plan_cannot_cross_user_or_conversation(flow, field, value):
    token = prepare(flow)
    setattr(flow, field, value)
    result = flow.module.create_delivery_note_draft(token)
    assert result["status"] == "error" and result["rolled_back"]
    flow.doc.insert.assert_not_called()


def test_expired_approval_does_not_create(flow):
    token = prepare(flow)
    flow.cache[flow.module._key("preview", token)]["expires_at"] = "2026-09-17 11:00:00"
    result = flow.module.create_delivery_note_draft(token)
    assert result["status"] == "needs_input" and "过期" in result["reason"]
    flow.doc.insert.assert_not_called()


def test_changed_receipt_or_draft_occupation_requires_new_review(flow):
    token = prepare(flow)
    flow.state["items"][0]["available_qty"] = 2
    result = flow.module.create_delivery_note_draft(token)
    assert result["status"] == "needs_input" and "已变化" in result["reason"]
    flow.doc.insert.assert_not_called()


def test_create_uses_normal_permission_and_reuses_same_approval(flow):
    token = prepare(flow)
    m = flow.module
    created = m.create_delivery_note_draft(token)
    assert created["status"] == "created" and created["docstatus"] == 0 and created["verified"]
    flow.doc.insert.assert_called_once_with()
    flow.doc.submit.assert_not_called()
    assert m._order_state.call_args.kwargs["for_update"] is True
    repeated = m.create_delivery_note_draft(token)
    assert repeated["status"] == "existing" and repeated["delivery_note"] == "DN-1"
    flow.doc.insert.assert_called_once_with()
    assert all(s["action"] and s["reason"] for s in created["steps"])


def test_failure_after_save_rolls_back_and_reports_saved_step_as_rolled_back(flow):
    token = prepare(flow)
    flow.readback_error = True
    result = flow.module.create_delivery_note_draft(token)
    assert result["status"] == "error" and result["rolled_back"] and result["failed_step"] == "verify"
    assert next(s for s in result["steps"] if s["key"] == "save")["status"] == "rolled_back"
    assert flow.frappe.db.rollback.call_args.kwargs["save_point"].startswith("delivery_create_")


def test_real_card_discloses_partial_payment_and_selected_quantity(flow):
    token = prepare(flow)
    card = flow.module._confirmation_prompt({"preview_token": token})
    assert "🟠" in card and "已收 15.00" in card and "未收 15.00" in card
    assert "4 件" in card and "大坪仓库" in card and "草稿" in card
    assert flow.module.create_delivery_note_draft.requires_confirmation is True


def test_deadlock_savepoint_loss_does_not_hide_original_error(flow):
    flow.frappe.db.rollback.side_effect = [Exception(1305, "SAVEPOINT does not exist"), None]
    flow.module._rollback("point")
    assert flow.frappe.db.rollback.call_args_list[0].kwargs == {"save_point": "point"}
    assert flow.frappe.db.rollback.call_args_list[1].kwargs == {}


class Row(dict):
    __getattr__ = dict.get


def bundle_order():
    return Row(items=[Row(name="so-bundle", item_code="BUNDLE", qty=10, stock_qty=20, warehouse="W")],
               packed_items=[Row(parent_detail_docname="so-bundle", parent_item="BUNDLE", item_code="CHALK",
                                 item_name="山东绿方", qty=20, uom="颗", warehouse="W"),
                             Row(parent_detail_docname="so-bundle", parent_item="BUNDLE", item_code="STICKER",
                                 item_name="客户绿方贴纸", qty=20, uom="张", warehouse="W")])


def test_bundle_options_scale_components_to_pending_quantity_without_double_conversion(flow):
    rows = [{"sales_order_item": "so-bundle", "available_qty": 3}]
    flow.module._add_bundle_components(bundle_order(), rows)
    assert [(r["item_code"], r["available_qty"], r["qty_per_unit"]) for r in rows[0]["components"]] == [
        ("CHALK", 6, 2), ("STICKER", 6, 2)]


@pytest.mark.parametrize("change", [None, "sticker", "qty", "warehouse", "parent"])
def test_delivery_bundle_must_keep_approved_order_pairing(flow, change):
    order = bundle_order()
    selected = [{"sales_order_item": "so-bundle", "available_qty": 3, "qty": 2}]
    flow.module._add_bundle_components(order, selected)
    doc = Row(items=[Row(name="dn-bundle", item_code="BUNDLE", so_detail="so-bundle")],
              packed_items=[Row(component, parent_detail_docname="dn-bundle", qty=4) for component in order["packed_items"]])
    if change == "sticker":
        doc["packed_items"][1]["item_code"] = "OTHER-STICKER"
    elif change == "qty":
        doc["packed_items"][1]["qty"] = 5
    elif change == "warehouse":
        doc["packed_items"][1]["warehouse"] = "OTHER-W"
    elif change == "parent":
        doc["packed_items"][1]["parent_detail_docname"] = "another-row"
    if change:
        with pytest.raises(ValueError):
            flow.module._validate_delivery_components(doc, selected)
    else:
        flow.module._validate_delivery_components(doc, selected)


def test_bundle_card_groups_components_under_selected_product(flow):
    token = prepare(flow)
    plan = flow.cache[flow.module._key("preview", token)]
    plan["summary"]["packed_items"] = [
        {"sales_order_item": "row-1", "item_code": "STICKER", "item_name": "客户绿方贴纸",
         "qty": 4, "uom": "张", "warehouse": "大坪仓库 - LEYA"}]
    card = flow.module._confirmation_prompt({"preview_token": token})
    assert "内含：客户绿方贴纸 × 4 张" in card
