"""Regression tests for a Flow-only release and its old-image rollback path."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


class Row(dict):
	def __getattr__(self, key):
		return self.get(key)


def load_release(monkeypatch):
	state = {
		"Flow Tool": [Row(name="custom", slug="custom", type="Script", code="return 'ok'", modified="before")],
		"Flow Agent": [Row(name="Flow", title="Flow", instructions="private guidance", model="original", modified="before"), Row(name="custom-agent", title="Custom", instructions="custom guidance", model="custom", modified="before")],
		"Flow Agent Tool": [Row(name="binding", parent="Flow", parenttype="Flow Agent", tool="custom", role=None), Row(name="custom-binding", parent="custom-agent", parenttype="Flow Agent", tool="custom", role="support")],
		"Flow Agent Knowledge Base": [Row(name="knowledge", parent="Flow", parenttype="Flow Agent", knowledge_base="sales")],
	}
	protected = {"Customer": "same", "permissions": "same", "sessions": "same", "credentials": "same"}
	frappe = types.ModuleType("frappe")
	frappe.local = types.SimpleNamespace(site="test")
	frappe.conf = Row(db_host="flow-check-db")
	frappe.clear_cache = lambda **_: None
	frappe.clear_document_cache = lambda *_: None
	frappe.get_installed_apps = lambda: ["erpnext", "erpnext_shipping"]

	def rows(doctype, filters=None, fields=None, order_by=None, pluck=None):
		data = state.get(doctype, [])
		for key, value in (filters or {}).items():
			data = [row for row in data if row.get(key) in value[1]] if isinstance(value, list) else [row for row in data if row.get(key) == value]
		return [row[pluck] for row in data] if pluck else copy.deepcopy(data)

	def delete(doctype, filters):
		target = {row["name"] for row in rows(doctype, filters)}
		state[doctype] = [row for row in state[doctype] if row["name"] not in target]

	def insert(doctype, fields, values):
		state.setdefault(doctype, []).extend(Row(zip(fields, row)) for row in values)

	commits = []
	frappe.get_all = rows
	frappe.get_meta = lambda dt: types.SimpleNamespace(get_table_fields=lambda: [Row(options="Flow Agent Tool"), Row(options="Flow Agent Knowledge Base")] if dt == "Flow Agent" else [])
	frappe.db = types.SimpleNamespace(table_exists=lambda dt: dt in state, delete=delete, bulk_insert=insert, commit=lambda: commits.append("commit"), rollback=lambda: commits.append("rollback"))
	response = types.ModuleType("frappe.utils.response")
	response.json_handler = str
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setitem(sys.modules, "frappe.utils.response", response)
	spec = importlib.util.spec_from_file_location("workflow_metadata_release", ROOT / "deploy/customer_service_workflows/metadata.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	monkeypatch.setattr(module, "_protected_state", lambda: copy.deepcopy(protected))
	return module, state, protected, commits


def test_snapshot_and_restore_use_no_new_flow_code_and_preserve_all_children(monkeypatch):
	module, state, _, commits = load_release(monkeypatch)
	snapshot = json.loads(module._encoded(module.capture()))
	state["Flow Tool"].append(Row(name="new", slug="new"))
	state["Flow Agent"][0]["instructions"] = "changed"
	state["Flow Agent Tool"].append(Row(name="new-binding", parent="Flow", parenttype="Flow Agent", tool="new"))
	state["Flow Agent Knowledge Base"][0]["knowledge_base"] = "changed"
	monkeypatch.setitem(sys.modules, "flow.integrations.erpnext.install", None)

	module.restore(snapshot)
	module.compare(snapshot)

	assert commits == ["commit"]
	assert state["Flow Agent Knowledge Base"][0]["knowledge_base"] == "sales"
	assert [row["slug"] for row in state["Flow Tool"]] == ["custom"]


def test_business_change_refuses_rollback_before_any_delete(monkeypatch):
	module, state, protected, commits = load_release(monkeypatch)
	snapshot = module.capture()
	protected["Customer"] = "someone changed customer"
	state_before = copy.deepcopy(state)

	with pytest.raises(AssertionError, match="protected_business_or_security_state_changed"):
		module.restore(snapshot)

	assert state == state_before
	assert commits == []


def test_repeated_migration_has_same_semantics_then_old_image_restore_is_exact(monkeypatch):
	module, state, _, _ = load_release(monkeypatch)
	snapshot = module.capture()
	def install():
		state["Flow Agent"][0]["instructions"] = "installed managed instructions"
		return {"installed": True}
	monkeypatch.setattr(module, "_install_workflows", install)
	monkeypatch.setattr(module, "_validate_contract", lambda snapshot: module._assert_protected_unchanged(snapshot))
	module.migrate(snapshot)
	first = module._semantic_flow(module._capture_flow())
	module.migrate(snapshot)
	assert module._semantic_flow(module._capture_flow()) == first
	module.restore(snapshot)
	assert module._encoded(module.capture()) == module._encoded(snapshot)


def test_failure_after_committed_metadata_exercises_real_restore(monkeypatch):
	module, state, _, commits = load_release(monkeypatch)
	snapshot = module.capture()
	monkeypatch.setattr(module, "_install_workflows", lambda: state["Flow Agent"][0].update(instructions="new"))
	monkeypatch.setattr(module, "_validate_contract", lambda snapshot: None)
	with pytest.raises(RuntimeError, match="ISOLATED_INJECTED_FAILURE_AFTER_OWNER"):
		module.migrate(snapshot, inject_failure=True)
	assert commits == ["commit"]
	assert state["Flow Agent"][0]["instructions"] == "new"
	module.restore(snapshot)
	assert state["Flow Agent"][0]["instructions"] == "private guidance"


def test_failure_injection_is_refused_for_production(monkeypatch):
	module, state, _, commits = load_release(monkeypatch)
	module.frappe.conf.db_host = "production-db"
	with pytest.raises(AssertionError, match="failure_injection_requires_isolated_database"):
		module.migrate(module.capture(), inject_failure=True)
	assert commits == []


def test_duplicate_managed_slug_is_not_silently_collapsed(monkeypatch):
	module, state, _, _ = load_release(monkeypatch)
	state["Flow Tool"] = [Row(name="a", slug="query_sales_order_details"), Row(name="b", slug="query_sales_order_details")]
	with pytest.raises(AssertionError, match="duplicate_managed_flow_tool_slug"):
		module._managed_rows()


def test_database_probe_errors_propagate(monkeypatch):
	module, _, _, _ = load_release(monkeypatch)
	def failed_probe(doctype):
		raise RuntimeError("database disconnected")
	module.frappe.db.table_exists = failed_probe
	with pytest.raises(RuntimeError, match="database disconnected"):
		module._table_exists("Customer")


def test_custom_agent_bindings_are_preserved_exactly(monkeypatch):
	module, state, _, _ = load_release(monkeypatch)
	module.FLOW_AGENT_TITLES = ()
	snapshot = module.capture()
	module._validate_preserved_metadata(snapshot)
	state["Flow Agent Tool"][1]["role"] = "changed"
	with pytest.raises(AssertionError, match="flow_agent_binding_changed:custom-agent"):
		module._validate_preserved_metadata(snapshot)
	state["Flow Agent Tool"][1]["role"] = "support"
	state["Flow Agent Tool"].append(Row(name="unexpected", parent="custom-agent", parenttype="Flow Agent", tool="custom"))
	with pytest.raises(AssertionError, match="non_target_agent_binding_added:custom-agent"):
		module._validate_preserved_metadata(snapshot)


def test_all_installed_guidance_is_allowed_but_unrelated_edits_are_rejected(monkeypatch):
	module, state, _, _ = load_release(monkeypatch)
	from flow.integrations.erpnext import (
		customer_install, sticker_install, sales_order_install, delivery_note_install,
		document_submission_install, finance_flow_install, inventory_install,
		sf_label_install, waybill_flow_install,
	)
	snapshot = module.capture()
	monkeypatch.setattr(module, "REQUIRED_AGENT_BINDING_SLUGS", set())
	transforms = (
		customer_install.with_customer_guidance, sticker_install.with_sticker_guidance,
		sales_order_install.with_sales_order_guidance, delivery_note_install.with_delivery_note_guidance,
		document_submission_install.with_document_submission_guidance,
		finance_flow_install.with_finance_guidance, inventory_install.with_inventory_guidance,
		sf_label_install.with_sf_label_guidance, waybill_flow_install.with_replacement_guidance,
	)
	for transform in transforms:
		state["Flow Agent"][0]["instructions"] = transform(state["Flow Agent"][0]["instructions"])
	module._validate_preserved_metadata(snapshot)
	first = state["Flow Agent"][0]["instructions"]
	for _ in range(3):
		for transform in transforms:
			state["Flow Agent"][0]["instructions"] = transform(state["Flow Agent"][0]["instructions"])
		assert state["Flow Agent"][0]["instructions"] == first
		module._validate_preserved_metadata(snapshot)
	state["Flow Agent"][0]["instructions"] += "\nunrelated injected instructions"
	with pytest.raises(AssertionError, match="flow_agent_guidance_changed_outside_contract"):
		module._validate_preserved_metadata(snapshot)


def query_fixture(monkeypatch):
	module, _, _, _ = load_release(monkeypatch)
	f = module.frappe
	f.session = Row(user="Administrator")
	f.flags = Row()
	f.PermissionError = PermissionError
	f.set_user = lambda user: f.session.update(user=user)
	f.get_all = lambda dt, **kw: ["sales@example.invalid"]
	f.get_list = lambda *a, **kw: [{"name": "order-private"}]
	items = [Row(name=f"row-{i}", idx=i, item_code=f"item-{i}", item_name="客户产品", qty=i,
		uom="颗", rate=3, amount=i * 3, description="完整描述" * 200,
		warehouse="原仓库", delivery_date="2026-10-07", delivered_qty=0,
		billed_amt=0, is_free_item=0) for i in range(1, 26)]
	order = Row(name="order-private", items=items)
	order.check_permission = lambda permission: None
	f.get_doc = lambda *a, **kw: order
	f.has_permission = lambda *a, **kw: f.session.user != "Guest"
	result = {"status": "found", "verified": True, "sales_order": order.name,
		"items": [{"sales_order_item": item.name, "row_no": item.idx,
			**{key: item[key] for key in ("item_code", "item_name", "qty", "uom", "rate", "amount", "description", "warehouse", "delivery_date", "delivered_qty", "billed_amt", "is_free_item")}}
			for item in items]}
	denied = {"status": "error", "verified": False, "items": []}
	calls = []
	def query(**kwargs):
		calls.append((f.session.user, kwargs))
		return copy.deepcopy(denied if f.session.user == "Guest" else result)
	def get_attr(path):
		assert path == "flow.integrations.erpnext.sales_order_flow.query_sales_order_details"
		return query
	f.get_attr = get_attr
	return module, result, denied, calls


def test_isolated_query_checks_complete_order_and_native_permission_without_details(monkeypatch):
	module, _, _, calls = query_fixture(monkeypatch)
	result = module._validate_sales_order_query()
	assert result == {"status": "passed", "orders_checked": 1, "items_checked": 25, "permission_denials_checked": 1}
	assert [user for user, _ in calls] == ["sales@example.invalid", "Guest"]
	assert "order-private" not in json.dumps(result)
	assert module.frappe.session.user == "Administrator"


@pytest.mark.parametrize("failure", ["truncate", "quantity", "description", "permission"])
def test_isolated_query_rejects_lost_rows_wrong_values_and_permission_bypass(monkeypatch, failure):
	module, result, denied, _ = query_fixture(monkeypatch)
	if failure == "truncate":
		result["items"].pop()
	elif failure == "quantity":
		result["items"][0]["qty"] = 999
	elif failure == "description":
		result["items"][0]["description"] = "截断"
	else:
		denied.update(result)
	with pytest.raises(AssertionError, match="sales_order_query_"):
		module._validate_sales_order_query()
	assert module.frappe.session.user == "Administrator"


def test_query_with_no_visible_order_is_unverified_not_passed(monkeypatch):
	module, _, _, calls = query_fixture(monkeypatch)
	module.frappe.get_list = lambda *a, **kw: []
	with pytest.raises(AssertionError, match="sales_order_query_unverified:no_visible_sales_order"):
		module._validate_sales_order_query()
	assert not calls
	assert module.frappe.session.user == "Administrator"


def test_query_without_sales_user_is_unverified_not_passed(monkeypatch):
	module, _, _, calls = query_fixture(monkeypatch)
	module.frappe.get_all = lambda *a, **kw: []
	with pytest.raises(AssertionError, match="sales_order_query_unverified:no_enabled_sales_user"):
		module._validate_sales_order_query()
	assert not calls


def test_production_path_does_not_import_or_execute_candidate_query(monkeypatch):
	module, _, _, _ = load_release(monkeypatch)
	module.frappe.conf.db_host = "production-db"
	assert module._validate_sales_order_query() == {"status": "not_run", "reason": "isolation_only", "orders_checked": 0}
