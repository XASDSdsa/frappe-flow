"""Only managed text may change; the saved plan enables exact old-image rollback."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import stat
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


class Row(dict):
	__getattr__ = dict.get


def load_release(monkeypatch):
	state = {
		"Flow Tool": [
			Row(name="known", slug="known", type="Imported", import_path="flow.tools.known", description="old",
				enabled=0, requires_confirmation=1, modified="before", modified_by="original", summary="Keep summary"),
			Row(name="custom", slug="custom", type="Script", code="custom code", description="custom description"),
		],
		"Flow Agent": [
			Row(name="agent", instructions="Private rule\nManaged: old\nPrivate ending", model="original", enabled=0),
			Row(name="custom", instructions="Entirely custom instructions", model="custom"),
			Row(name="empty", instructions=None, model="empty"),
		],
		"Flow Agent Tool": [Row(name="binding", parent="agent", parenttype="Flow Agent", tool="known", role="Support", idx=1)],
		"Flow Agent Knowledge Base": [Row(name="knowledge", parent="agent", parenttype="Flow Agent", knowledge_base="sales")],
	}
	protected = {"Customer": "unchanged", "sessions": "unchanged", "permissions": "unchanged"}
	frappe = types.ModuleType("frappe")
	frappe.local = Row(site="test")
	frappe.conf = Row(db_host="flow-check-db")
	frappe.get_app_path = lambda app: str(ROOT / app)
	writes, transactions, cleared, locks = [], [], [], []
	frappe.clear_document_cache = lambda *args: cleared.append(args)

	def get_all(doctype, filters=None, **kwargs):
		rows = state.get(doctype, [])
		for key, value in (filters or {}).items():
			rows = [row for row in rows if row.get(key) == value]
		return copy.deepcopy(rows)

	baseline = None
	def sql(query, args):
		nonlocal baseline
		if baseline is None:
			baseline = copy.deepcopy(state)
		match = re.fullmatch(r"SELECT `([^`]+)` FROM `tab([^`]+)` WHERE name=%s FOR UPDATE", query)
		assert match, query
		field, doctype = match.groups()
		locks.append((doctype, args[0], field))
		return [(row.get(field),) for row in state[doctype] if row["name"] == args[0]]

	def set_value(doctype, name, field, value, *, update_modified):
		writes.append((doctype, name, field, value, update_modified))
		next(row for row in state[doctype] if row["name"] == name)[field] = value

	def commit():
		nonlocal baseline
		transactions.append("commit")
		baseline = None

	def rollback():
		nonlocal baseline
		transactions.append("rollback")
		if baseline is not None:
			state.clear()
			state.update(baseline)
		baseline = None

	frappe.get_all = get_all
	frappe.get_meta = lambda dt: types.SimpleNamespace(get_table_fields=lambda: [
		Row(options="Flow Agent Tool"), Row(options="Flow Agent Knowledge Base"),
	] if dt == "Flow Agent" else [])
	frappe.db = types.SimpleNamespace(table_exists=lambda dt: dt in state, set_value=set_value,
		sql=sql, commit=commit, rollback=rollback)
	response = types.ModuleType("frappe.utils.response")
	response.json_handler = str
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setitem(sys.modules, "frappe.utils.response", response)
	source = types.ModuleType("flow.integrations.erpnext.markdown_guidance")
	source.description_definitions = lambda: [
		{"slug": "known", "import_path": "flow.tools.known", "description": "Summary.\n\n- Details."},
		{"slug": "optional", "import_path": "flow.tools.optional", "description": "Optional tool."},
	]
	source.refresh_existing_guidance = lambda text: text.replace("Managed: old", "Managed:\n- Current rule.")
	monkeypatch.setitem(sys.modules, source.__name__, source)
	spec = importlib.util.spec_from_file_location("markdown_metadata_release", ROOT / "deploy/markdown_guidance/metadata.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	monkeypatch.setattr(module._library(), "_protected_state", lambda: copy.deepcopy(protected))
	return module, state, protected, writes, transactions, cleared, locks


def test_only_text_changes_and_second_migration_writes_nothing(monkeypatch):
	module, state, _, writes, transactions, cleared, locks = load_release(monkeypatch)
	snapshot = module.capture()
	plan = module.prepare(snapshot)
	assert plan["missing_tools_skipped"] == 1 and len(plan["changes"]) == 2
	result = module.migrate(snapshot, plan)
	expected = copy.deepcopy(snapshot)
	expected["flow"][0]["rows"][0]["description"] = "Summary.\n\n- Details."
	expected["flow"][1]["rows"][0]["instructions"] = "Private rule\nManaged:\n- Current rule.\nPrivate ending"
	assert module.capture() == expected
	assert result == {"descriptions_updated": 1, "instructions_updated": 1, "missing_tools_skipped": 1}
	assert module.validate(snapshot, plan) == {"status": "verified", "planned_fields": 2}
	assert module.migrate(snapshot, plan) == {"descriptions_updated": 0, "instructions_updated": 0, "missing_tools_skipped": 1}
	assert len(writes) == 2 and all(write[-1] is False for write in writes)
	assert {write[:3] for write in writes} == {("Flow Tool", "known", "description"), ("Flow Agent", "agent", "instructions")}
	assert transactions == ["commit", "commit"]
	assert len(locks) == len(cleared) == 4


def test_saved_plan_restores_without_candidate_imports(monkeypatch, tmp_path):
	module, _, _, writes, transactions, _, _ = load_release(monkeypatch)
	snapshot = module.capture()
	path = tmp_path / "snapshot.json.markdown-plan.json"
	module._save_private(path, module.prepare(snapshot))
	plan = json.loads(path.read_text())
	assert stat.S_IMODE(path.stat().st_mode) == 0o600
	with pytest.raises(AssertionError, match="metadata_file_exists"):
		module._save_private(path, {})
	module.migrate(snapshot, plan)
	monkeypatch.setitem(sys.modules, "flow.integrations.erpnext.markdown_guidance", None)
	module.restore(snapshot, plan)
	module.compare(snapshot)
	assert module.capture() == snapshot
	assert len(writes) == 4 and transactions == ["commit", "commit"]


@pytest.mark.parametrize("scope", ["Flow Tool", "Flow Agent", "Flow Agent Tool", "Flow Agent Knowledge Base", "protected"])
def test_unrelated_drift_blocks_migrate_and_restore(monkeypatch, scope):
	module, state, protected, writes, _, _, _ = load_release(monkeypatch)
	snapshot = module.capture()
	plan = module.prepare(snapshot)
	if scope == "protected":
		protected["Customer"] = "changed"
	else:
		state[scope][0]["unrelated"] = "changed"
	for action in (module.migrate, module.restore):
		with pytest.raises(AssertionError, match="changed"):
			action(snapshot, plan)
	assert not writes


@pytest.mark.parametrize("doctype,field", [("Flow Tool", "description"), ("Flow Agent", "instructions")])
def test_concurrent_target_edit_blocks_migrate_and_restore(monkeypatch, doctype, field):
	module, state, _, writes, _, _, _ = load_release(monkeypatch)
	snapshot = module.capture()
	plan = module.prepare(snapshot)
	state[doctype][0][field] = "Concurrent custom change"
	for action in (module.migrate, module.restore):
		with pytest.raises(AssertionError, match="managed_text_changed_since_snapshot"):
			action(snapshot, plan)
	assert not writes and state[doctype][0][field] == "Concurrent custom change"


@pytest.mark.parametrize("change", ["duplicate", "script", "wrong_import"])
def test_ambiguous_or_customized_registration_is_rejected(monkeypatch, change):
	module, state, _, writes, transactions, _, _ = load_release(monkeypatch)
	if change == "duplicate":
		state["Flow Tool"].append(copy.deepcopy(state["Flow Tool"][0]))
	elif change == "script":
		state["Flow Tool"][0]["type"] = "Script"
	else:
		state["Flow Tool"][0]["import_path"] = "custom.known"
	with pytest.raises(AssertionError, match="managed_tool"):
		module.prepare(module.capture())
	assert not writes and not transactions


def test_validation_failure_rolls_back_all_pending_text(monkeypatch):
	module, state, _, writes, transactions, _, _ = load_release(monkeypatch)
	snapshot = module.capture()
	plan = module.prepare(snapshot)
	set_value = module.frappe.db.set_value
	def corrupt(*args, **kwargs):
		set_value(*args, **kwargs)
		state["Flow Agent Tool"][0]["role"] = "unintended change"
	monkeypatch.setattr(module.frappe.db, "set_value", corrupt)
	with pytest.raises(AssertionError, match="outside_markdown_plan"):
		module.migrate(snapshot, plan)
	assert module.capture() == snapshot
	assert len(writes) == 2 and transactions == ["rollback"]


def test_isolated_post_commit_failure_has_exact_old_image_rollback(monkeypatch):
	module, _, _, _, transactions, _, _ = load_release(monkeypatch)
	snapshot = module.capture()
	plan = module.prepare(snapshot)
	with pytest.raises(RuntimeError, match="ISOLATED_INJECTED_FAILURE_AFTER_OWNER:FLOW_METADATA"):
		module.migrate(snapshot, plan, inject_failure=True)
	assert transactions == ["commit"] and module.capture() != snapshot
	monkeypatch.setitem(sys.modules, "flow.integrations.erpnext.markdown_guidance", None)
	module.restore(snapshot, plan)
	assert module.capture() == snapshot and transactions == ["commit", "commit"]


def test_production_refuses_failure_injection_before_writes(monkeypatch):
	module, _, _, writes, transactions, _, _ = load_release(monkeypatch)
	snapshot = module.capture()
	plan = module.prepare(snapshot)
	module.frappe.conf.db_host = "production-db"
	with pytest.raises(AssertionError, match="failure_injection_requires_isolated_database"):
		module.migrate(snapshot, plan, inject_failure=True)
	assert not writes and not transactions


def test_plan_drift_and_forbidden_fields_are_rejected(monkeypatch):
	module, _, _, writes, transactions, _, _ = load_release(monkeypatch)
	snapshot = module.capture()
	plan = module.prepare(snapshot)
	changed = copy.deepcopy(plan)
	changed["changes"][0]["after"] = "Unreviewed change"
	with pytest.raises(AssertionError, match="candidate_mismatch"):
		module.migrate(snapshot, changed)
	changed["changes"][0]["field"] = "enabled"
	with pytest.raises(AssertionError, match="plan_field_invalid"):
		module.restore(snapshot, changed)
	changed = copy.deepcopy(plan)
	changed["snapshot_sha256"] = "wrong snapshot"
	with pytest.raises(AssertionError, match="plan_snapshot_mismatch"):
		module.restore(snapshot, changed)
	assert not writes and not transactions
