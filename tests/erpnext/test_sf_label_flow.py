"""Offline review boundary, idempotency and external-effect failure tests."""
from copy import deepcopy
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
PROVIDER = "erpnext_shipping.sf_international."
SHIPPING_ROOT = Path(pytest.importorskip("erpnext_shipping").__file__).parent


class Doc(dict):
    def __getattr__(self, key):
        return self.get(key)

    def __setattr__(self, key, value):
        self[key] = value

    def set(self, key, value):
        self[key] = value


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(monkeypatch):
    h = Doc(user="sales@example.com", scope="chat1", message="m1", cache={}, ledgers={})
    f = types.ModuleType("frappe")
    f.PermissionError = PermissionError
    f.local = Doc(site="test.local")
    f.flags = Doc()
    f.cache = Doc(get_value=lambda k: deepcopy(h.cache.get(k)),
                  set_value=lambda k, v, **kw: h.cache.__setitem__(k, deepcopy(v)))
    f.db = Doc(savepoint=Mock(), rollback=Mock(), commit=Mock(),
               exists=lambda dt, name: name in h.ledgers, get_value=Mock(return_value=None))
    f.enqueue = Mock()
    util = types.ModuleType("frappe.utils")
    util.now_datetime = lambda: datetime(2026, 9, 17, 12)
    util.get_datetime = datetime.fromisoformat
    util.strip_html = str
    monkeypatch.setitem(sys.modules, "frappe", f)
    monkeypatch.setitem(sys.modules, "frappe.utils", util)
    shipping = types.ModuleType(PROVIDER + "shipping")
    shipping.SF_PROVIDER = "SF International"
    shipping._last_sf_receiver_for_address = Mock(return_value=None)
    shipping._saved_sf_receiver = Mock(return_value=None)
    shipping.lookup_sf_postcode = Mock(return_value=[dict(country="US", province="TX", city="San Antonio", county="Bexar", post_code="78216")])
    shipping._sf_waybill = lambda doc: doc.get("shipment_id") or doc.get("awb_number") or ""
    shipping._is_sf_shipment = lambda doc: doc.get("carrier") in (None, "SF International")
    shipping._is_cancelled = lambda doc: doc.get("status") == "Cancelled"
    shipping._ensure_official_fields_from_sf = Mock()
    shipping._create_order_body_from_form = lambda doc, form: ({"pieceorderBaseInfo": {"userOrderid": doc.name}, "form": deepcopy(form)}, "10", "国际小包")
    shipping._booking_form_for_doc = lambda doc: json.loads(doc.sf_form_json)
    shipping.book_sf_order_after_commit = Mock(return_value="SF123")
    shipping.print_sf_label = Mock(return_value="/private/files/label.pdf")
    shipping.dispatch_sf_shipment = Mock(return_value="/private/files/label.pdf")
    monkeypatch.setitem(sys.modules, PROVIDER + "shipping", shipping)
    # Relative import from package must resolve this per-test stub too.
    package = types.ModuleType(PROVIDER[:-1]); package.__path__ = []
    package.shipping = shipping
    monkeypatch.setitem(sys.modules, PROVIDER[:-1], package)
    helpers = types.ModuleType(PACKAGE + "sales_order_flow")
    helpers._actor = lambda: h.user
    helpers._scope = lambda: h.scope
    helpers._json = lambda v: json.dumps(v, sort_keys=True, default=str, ensure_ascii=False)
    helpers._hash = lambda v: hashlib.sha256(helpers._json(v).encode()).hexdigest()
    monkeypatch.setitem(sys.modules, PACKAGE + "sales_order_flow", helpers)
    delivery = types.ModuleType(PACKAGE + "delivery_note_flow")
    delivery._last_customer_message = lambda scope: h.message
    delivery._rollback = lambda point: f.db.rollback(save_point=point)
    monkeypatch.setitem(sys.modules, PACKAGE + "delivery_note_flow", delivery)
    rules = load(PROVIDER + "sf_label_rules", SHIPPING_ROOT / "sf_international/sf_label_rules.py")
    monkeypatch.setitem(sys.modules, PROVIDER + "sf_label_rules", rules)
    booking = load(PROVIDER + "reviewed_booking", SHIPPING_ROOT / "sf_international/reviewed_booking.py")
    monkeypatch.setitem(sys.modules, PROVIDER + "reviewed_booking", booking)
    tool = types.ModuleType("flow.lib.tool")
    def decorate(fn, **kw):
        fn.__dict__.update(kw)
        return fn
    tool.tool = decorate
    monkeypatch.setitem(sys.modules, "flow.lib.tool", tool)
    m = load(PACKAGE + "sf_label_flow", "flow/integrations/erpnext/sf_label_flow.py")
    h.parcels = [dict(length=30, width=20, height=15, weight=2.5, count=1)]
    h.state = dict(delivery_note="DN-1", dn_modified="2026-09-17 10:00:00", customer="C1", customer_name="客户", company="Co",
        shipment=None, shipment_modified=None, address_name="ADDRESS-1",
        receiver=dict(country="US", province="Texas", city="San Antonio", county="", post_code="78216", address="239 Sharon Dr", contact="Current", phone="+1 2107607172"),
        sender=dict(country="CN", company="Company", contact="Sender", phone="123", province="湖北", city="咸宁", address="Street"),
        parcels=deepcopy(h.parcels), product_code="10", products=[dict(product_code="10", product_name="国际小包", is_preferred=1)],
        customs=dict(declared_value=20, declared_currency="CNY", purchase_currency="CNY", hs_code="123", ename="Chalk", cname="巧克粉"),
        goods=[dict(item_code="P1", item_name="产品", qty=10, uom="Nos", warehouse="大坪仓库 - LEYA")])
    h.doc = Doc(name="new-shipment", docstatus=0, flags=Doc(), check_permission=Mock(),
                shipment_parcel=deepcopy(h.parcels), delivery_address_name="ADDRESS-1", delivery_customer="C1", pickup_company="Co",
                shipment_delivery_note=[Doc(delivery_note="DN-1")])
    def insert():
        h.doc.name = "SHIP-1"
    h.doc.insert = Mock(side_effect=insert)
    h.doc.save = Mock()
    h.doc.submit = Mock(side_effect=lambda: h.doc.update(docstatus=1))
    h.dn = Doc(name="DN-1", docstatus=1, modified=h.state["dn_modified"], check_permission=Mock())
    h.source = m._source = Mock(side_effect=lambda *a, **kw: (h.dn, h.doc, deepcopy(h.state)))
    h.attempt = Doc(name="ATTEMPT-1", replacement_reason=m.REASON)
    waybill = types.ModuleType(PROVIDER + "waybill")
    waybill._shipment_is_shipped = lambda doc: doc.get("status") in ("已发货", "Completed")
    def begin(doc, form, **kwargs):
        h.attempt.form_payload = helpers._json(form)
        return h.attempt
    waybill.begin_initial_booking_attempt = Mock(side_effect=begin)
    monkeypatch.setitem(sys.modules, PROVIDER + "waybill", waybill)
    def get_doc(doctype, name=None, **kwargs):
        if isinstance(doctype, dict):
            doc = Doc(doctype); doc.flags = Doc()
            def save_ledger(**kwargs):
                doc.name = doc.flags._name or doc.name
                h.ledgers[doc.name] = doc
            doc.insert = Mock(side_effect=save_ledger)
            doc.save = Mock(side_effect=save_ledger)
            return doc
        return {"Shipment": h.doc, "Delivery Note": h.dn, "SF Waybill": h.attempt,
                "Integration Request": h.ledgers.get(name)}[doctype]
    f.get_doc = get_doc
    shipping._sender_from_warehouse = Mock(return_value=h.state["sender"])
    shipping._party_from_address = Mock(return_value=h.state["receiver"])
    shipping._fill_receiver_gaps = lambda receiver, **kw: receiver
    shipping.is_enabled = lambda: True
    h.update(module=m, frappe=f, shipping=shipping, waybill=waybill)
    return h


def preview(h):
    result = h.module.prepare_sf_label("DN-1")
    assert result["status"] == "needs_selection", result
    h.message = "m2"
    result = h.module.preview_sf_label(result["preparation_token"], "A1")
    assert result["status"] == "preview", result
    return result["preview_token"]


def queued(h):
    token = preview(h)
    result = h.module._create_sf_label(token)
    assert result["status"] == "queued", result
    return token, next(iter(h.ledgers.values()))


def test_single_history_still_requires_later_human_choice(env):
    h = env
    h.shipping._last_sf_receiver_for_address.return_value = {**h.state["receiver"], "contact": "Old", "phone": "old"}
    r = h.module.prepare_sf_label("DN-1")
    assert r["choices"][0]["source"] == "history"
    h.shipping.lookup_sf_postcode.assert_not_called()
    p = h.module.preview_sf_label(r["preparation_token"], "A1")
    assert "尚未回复选择" in p["reason"]
    h.message = "m2"
    p = h.module.preview_sf_label(r["preparation_token"], "A1")
    assert p["summary"]["receiver"]["contact"] == "Current"
    assert p["summary"]["receiver"]["phone"] == "+1 2107607172"


def test_reject_fabricated_candidate_and_no_write_in_preview(env):
    h = env
    r = h.module.prepare_sf_label("DN-1"); h.message = "m2"
    p = h.module.preview_sf_label(r["preparation_token"], "FAKE")
    assert p["status"] == "needs_input"
    h.doc.insert.assert_not_called(); h.shipping.book_sf_order_after_commit.assert_not_called()


def test_only_matching_api_rows_and_no_automatic_history_on_new_street(env):
    h = env
    h.shipping.lookup_sf_postcode.return_value += [dict(country="US", province="TX", city="Wrong", post_code="12345")]
    r = h.module.prepare_sf_label("DN-1")
    assert r["total_choices"] == 1
    assert r["choices"][0]["city"] == "San Antonio"


def test_paging_retains_real_ids_without_exposing_unseen_choice(env):
    h = env
    h.shipping.lookup_sf_postcode.return_value = [dict(country="US", province="TX", city=f"City{i}", post_code="78216") for i in range(14)]
    r = h.module.prepare_sf_label("DN-1"); h.message = "m2"
    assert r["next_start"] == 12
    assert h.module.preview_sf_label(r["preparation_token"], "A14")["status"] == "needs_input"
    h.module.list_sf_label_addresses(r["preparation_token"], 12)
    assert "尚未回复选择" in h.module.preview_sf_label(r["preparation_token"], "A14")["reason"]
    h.message = "m3"
    assert h.module.preview_sf_label(r["preparation_token"], "A14")["status"] == "preview"
    assert h.shipping.lookup_sf_postcode.call_count == 1


def test_missing_parcels_do_not_become_default_weight(env):
    h = env; h.state["parcels"] = []
    r = h.module.prepare_sf_label("DN-1"); h.message = "m2"
    assert r["missing"]
    p = h.module.preview_sf_label(r["preparation_token"], "A1")
    assert "parcels" in p["missing"]
    h.doc.insert.assert_not_called()


def test_collects_missing_receiver_and_package_together(env):
    h = env; h.state["parcels"] = []; h.state["receiver"]["phone"] = ""
    r = h.module.prepare_sf_label("DN-1"); h.message = "m2"
    p = h.module.preview_sf_label(r["preparation_token"], "A1")
    assert {"phone", "parcels"} <= set(p["missing"])


def test_unknown_product_or_geography_override_rejected(env):
    h = env
    r = h.module.prepare_sf_label("DN-1"); h.message = "m2"
    p = h.module.preview_sf_label(r["preparation_token"], "A1", product_code="fake", receiver_details={"city": "invented"})
    assert {"product_code", "city"} <= set(p["missing"])


@pytest.mark.parametrize("change", ["user", "scope"])
def test_plan_bound_to_actor_and_chat(env, change):
    token = preview(env)
    env[change] = "different"
    assert env.module._create_sf_label(token)["status"] == "blocked"
    env.doc.insert.assert_not_called()


def test_expired_plan_rejected(env):
    token = preview(env)
    env.cache[env.module._key("preview", token)]["expires_at"] = "2026-09-17 11:00:00"
    assert "过期" in env.module._create_sf_label(token)["reason"]
    env.frappe.enqueue.assert_not_called()


def test_changed_source_after_approval_prevents_save(env):
    token = preview(env); env.state["receiver"]["address"] = "Other street"
    result = env.module._create_sf_label(token)
    assert "审批期间" in result["reason"]
    env.doc.insert.assert_not_called(); env.frappe.enqueue.assert_not_called()


def test_one_approval_enqueues_only_after_all_local_writes(env):
    h = env
    token, ledger = queued(h)
    assert h.doc.flags.flow_sf_defer_booking
    h.frappe.enqueue.assert_called_once()
    assert h.frappe.enqueue.call_args.kwargs["enqueue_after_commit"] is True
    h.shipping.book_sf_order_after_commit.assert_not_called()
    assert h.module.create_sf_label.requires_confirmation is True
    prompt = h.module.create_sf_label.confirm_prompt({"preview_token": token})
    assert all(s in prompt for s in ["申报金额", "收件人", "239 Sharon Dr", "2.5", "发件地址", "批准一次"])


def test_readback_mismatch_rolls_back_without_queue(env):
    h = env; token = preview(h)
    h.doc.submit.side_effect = lambda: h.doc.update(docstatus=0)
    result = h.module._create_sf_label(token)
    assert result["status"] == "needs_input"
    h.frappe.db.rollback.assert_called_once(); h.frappe.enqueue.assert_not_called()


def test_repeat_approval_returns_original_without_new_booking(env):
    h = env; token, ledger = queued(h)
    h.frappe.db.get_value.return_value = ledger.name
    r = h.module._create_sf_label(token)
    assert r["shipment"] == "SHIP-1"
    assert h.doc.insert.call_count == 1
    assert h.frappe.enqueue.call_count == 1


def test_worker_claim_is_durable_and_replay_never_posts_twice(env):
    h = env; token, ledger = queued(h)
    h.module.validate_reviewed_booking = Mock()
    h.module.finish_sf_label(ledger.name)
    assert json.loads(ledger.output)["status"] == "ready"
    assert h.frappe.db.commit.call_count >= 2
    h.module.finish_sf_label(ledger.name)
    h.shipping.book_sf_order_after_commit.assert_called_once()
    h.shipping.print_sf_label.assert_called_once()


def test_worker_preflight_changed_source_never_calls_carrier(env):
    h = env; token, ledger = queued(h)
    h.frappe.db.get_value.return_value = ledger.name
    h.dn.modified = "2026-09-17 11:00:00"
    h.module.finish_sf_label(ledger.name)
    assert json.loads(ledger.output)["status"] == "needs_review"
    h.shipping.book_sf_order_after_commit.assert_not_called()
    h.shipping.print_sf_label.assert_not_called()


def test_carrier_timeout_returns_uncertainty_and_never_retries(env):
    h = env; token, ledger = queued(h)
    h.module.validate_reviewed_booking = Mock()
    h.shipping.book_sf_order_after_commit.side_effect = TimeoutError("网络超时")
    h.module.finish_sf_label(ledger.name); h.module.finish_sf_label(ledger.name)
    assert json.loads(ledger.output)["status"] == "uncertain"
    assert "网络超时" in ledger.output
    h.shipping.book_sf_order_after_commit.assert_called_once()
    h.shipping.print_sf_label.assert_not_called()


def test_pdf_failure_preserves_created_waybill_and_result_is_read_only(env):
    h = env; token, ledger = queued(h)
    h.module.validate_reviewed_booking = Mock()
    def book(*args, **kwargs):
        h.doc.shipment_id = "SF123"
        return "SF123"
    h.shipping.book_sf_order_after_commit.side_effect = book
    h.shipping.print_sf_label.side_effect = RuntimeError("PDF unavailable")
    h.module.finish_sf_label(ledger.name)
    assert json.loads(ledger.output)["status"] == "created_without_pdf"
    h.frappe.db.get_value.return_value = ledger.name
    r = h.module.get_sf_label_result("SHIP-1")
    assert r["verified"] and r["waybill"] == "SF123" and not r["label_url"]
    assert "PDF unavailable" in r["reason"]
    h.shipping.book_sf_order_after_commit.assert_called_once(); h.shipping.print_sf_label.assert_called_once()


def test_native_worker_cannot_bypass_flow_claim(env):
    h = env; token, ledger = queued(h)
    h.frappe.db.get_value.return_value = ledger.name
    with pytest.raises(h.module.LabelInputError, match="Flow 后台"):
        h.module.validate_reviewed_booking(h.doc, json.loads(h.doc.sf_form_json), h.attempt)


def test_worker_rechecks_current_form_not_only_original_attempt(env):
    h = env; token, ledger = queued(h)
    h.frappe.db.get_value.return_value = ledger.name
    form = json.loads(h.doc.sf_form_json)
    form['receiver']['phone'] = 'Changed phone'
    h.doc.sf_form_json = json.dumps(form)
    h.module.finish_sf_label(ledger.name)
    assert json.loads(ledger.output)['status'] == 'needs_review'
    h.shipping.book_sf_order_after_commit.assert_not_called()


def test_revoked_write_permission_blocks_carrier(env):
    h = env; token, ledger = queued(h)
    h.frappe.db.get_value.return_value = ledger.name
    def permission(kind):
        if kind == 'write':
            raise PermissionError('写入权限已撤回')
    h.doc.check_permission.side_effect = permission
    h.module.finish_sf_label(ledger.name)
    assert '写入权限已撤回' in ledger.output
    h.shipping.book_sf_order_after_commit.assert_not_called()


def test_result_never_reports_other_carrier_as_sf_success(env):
    h = env; h.doc.carrier = 'DHL'; h.doc.shipment_id = 'DHL123'
    r = h.module.get_sf_label_result('SHIP-1')
    assert not r['verified'] and '不是顺丰' in r['reason']


def test_stale_ready_ledger_without_waybill_is_uncertain(env):
    h = env; token, ledger = queued(h)
    ledger.output = json.dumps({'status': 'ready', 'reason': 'previous result'})
    h.frappe.db.get_value.return_value = ledger.name
    r = h.module.get_sf_label_result('SHIP-1')
    assert r['status'] == 'uncertain' and not r['verified']


def test_read_failure_does_not_claim_no_external_booking(env):
    h = env; h.doc.check_permission.side_effect = PermissionError('没有读取权限')
    r = h.module.get_sf_label_result('SHIP-1')
    assert '尚未向顺丰' not in r['message']
    assert '没有读取权限' in r['reason']


def test_dispatch_existing_label_marks_shipped_and_reads_back(env):
    h = env
    h.doc.shipment_id = "SF123"
    h.doc.docstatus = 1
    h.doc.status = "待打单发货"
    h.shipping._is_sf_shipment = lambda doc: True
    h.shipping.dispatch_sf_shipment.side_effect = lambda name: (h.doc.update(status="已发货") or "/private/files/label.pdf")
    result = h.module._dispatch_sf_label("SHIP-1")
    assert result["status"] == "dispatched" and result["verified"]
    assert result["waybill"] == "SF123"
    h.shipping.dispatch_sf_shipment.assert_called_once_with("new-shipment")


def test_dispatch_replay_returns_existing_state_without_carrier_call(env):
    h = env
    h.doc.shipment_id = "SF123"
    h.doc.docstatus = 1
    h.doc.status = "已发货"
    h.shipping._is_sf_shipment = lambda doc: True
    result = h.module._dispatch_sf_label("SHIP-1")
    assert result["status"] == "already_shipped" and result["verified"]
    h.shipping.dispatch_sf_shipment.assert_not_called()


def test_dispatch_without_waybill_does_not_mutate(env):
    h = env
    h.doc.docstatus = 1
    h.doc.status = "待打单发货"
    h.shipping._is_sf_shipment = lambda doc: True
    result = h.module._dispatch_sf_label("SHIP-1")
    assert result["status"] == "error" and "真实顺丰单号" in result["reason"]
    h.shipping.dispatch_sf_shipment.assert_not_called()


def test_dispatch_post_call_failure_is_uncertain_and_never_claims_clean_failure(env):
    h = env
    h.doc.shipment_id = "SF123"
    h.doc.docstatus = 1
    h.doc.status = "待打单发货"
    h.shipping._is_sf_shipment = lambda doc: True

    def dispatch_then_fail(_name):
        h.doc.status = "待打单发货"
        raise TimeoutError("回读超时")

    h.shipping.dispatch_sf_shipment.side_effect = dispatch_then_fail
    result = h.module._dispatch_sf_label("SHIP-1")
    assert result["status"] == "uncertain" and not result["verified"]
    assert "不要重复点击" in result["message"]
