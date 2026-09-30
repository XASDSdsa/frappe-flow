"""Verify the one-field image metadata release and its old-image rollback."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


class Row(dict):
	def __getattr__(self, key):
		return self.get(key)


def load_release(monkeypatch):
	state = {
		"Flow Tool": [
			Row(name="image-tool", slug="show_image", type="Imported", import_path="flow.tools.builtins.show_image",
				description="old description", enabled=0, requires_confirmation=1, modified="before", modified_by="original"),
			Row(name="custom", slug="custom", type="Script", code="custom code", description="custom description"),
		],
		"Flow Agent": [Row(name="agent", instructions="private guidance", model="original")],
		"Flow Agent Tool": [Row(name="binding", parent="agent", parenttype="Flow Agent", tool="image-tool", role="Support")],
		"Flow Agent Knowledge Base": [Row(name="knowledge", parent="agent", parenttype="Flow Agent", knowledge_base="sales")],
	}
	protected = {"File": "same", "User": "same", "Customer": "same", "permissions": "same", "sessions": "same"}
	frappe = types.ModuleType("frappe")
	frappe.local = Row(site="test")
	frappe.conf = Row(db_host="production-db")
	frappe.session = Row(user="Administrator")
	frappe.flags = Row()
	frappe.PermissionError = PermissionError
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe._ = lambda message: message
	frappe.get_app_path = lambda app: str(ROOT / app)
	frappe.set_user = lambda user: frappe.session.update(user=user)
	cleared = []
	frappe.clear_document_cache = lambda *args: cleared.append(args)

	def rows(doctype, filters=None, **kwargs):
		data = state.get(doctype, [])
		for key, value in (filters or {}).items():
			data = [row for row in data if row.get(key) == value]
		return copy.deepcopy(data)

	writes, transactions = [], []
	committed = copy.deepcopy(state)
	def set_value(doctype, name, field, value, *, update_modified):
		writes.append((doctype, name, field, value, update_modified))
		next(row for row in state[doctype] if row["name"] == name)[field] = value
	def commit():
		nonlocal committed
		transactions.append("commit")
		committed = copy.deepcopy(state)
	def rollback():
		transactions.append("rollback")
		state.clear()
		state.update(copy.deepcopy(committed))

	frappe.get_all = rows
	frappe.get_meta = lambda dt: types.SimpleNamespace(get_table_fields=lambda: [
		Row(options="Flow Agent Tool"), Row(options="Flow Agent Knowledge Base"),
	] if dt == "Flow Agent" else [])
	frappe.db = types.SimpleNamespace(
		table_exists=lambda dt: dt in state, set_value=set_value, commit=commit, rollback=rollback,
	)
	response = types.ModuleType("frappe.utils.response")
	response.json_handler = str
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setitem(sys.modules, "frappe.utils.response", response)
	images = types.ModuleType("flow.tools.images")
	images.show_image = types.SimpleNamespace(description="candidate description")
	monkeypatch.setitem(sys.modules, "flow.tools.images", images)
	spec = importlib.util.spec_from_file_location("image_metadata_release", ROOT / "deploy/image_attachment_resolution/metadata.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	monkeypatch.setattr(module._library(), "_protected_state", lambda: copy.deepcopy(protected))
	return module, state, protected, writes, transactions, cleared


def test_migration_changes_only_description_and_is_idempotent(monkeypatch):
	module, state, _, writes, transactions, cleared = load_release(monkeypatch)
	snapshot = json.loads(module._library()._encoded(module.capture()))
	result = module.migrate(snapshot)
	first = module.capture()
	second = module.migrate(snapshot)
	expected = copy.deepcopy(snapshot)
	expected["flow"][0]["rows"][0]["description"] = "candidate description"
	assert first == module.capture() == expected
	assert result["descriptions_updated"] == 1
	assert second["descriptions_updated"] == 0
	assert writes == [("Flow Tool", "image-tool", "description", "candidate description", False)]
	assert transactions == ["commit", "commit"]
	assert cleared == [("Flow Tool", "image-tool")] * 2
	assert state["Flow Tool"][0]["enabled"] == 0
	assert state["Flow Tool"][0]["requires_confirmation"] == 1


def test_old_image_restore_and_compare_need_no_candidate_module(monkeypatch):
	module, _, _, writes, transactions, _ = load_release(monkeypatch)
	snapshot = module.capture()
	module.migrate(snapshot)
	monkeypatch.setitem(sys.modules, "flow.tools.images", None)
	monkeypatch.setitem(sys.modules, "flow.api.media", None)
	module.restore(snapshot)
	module.compare(snapshot)
	assert module.capture() == snapshot
	assert writes[-1] == ("Flow Tool", "image-tool", "description", "old description", False)
	assert transactions == ["commit", "commit"]


@pytest.mark.parametrize("scope", ["Flow Agent", "Flow Agent Tool", "Flow Agent Knowledge Base", "Flow Tool", "protected"])
def test_unrelated_metadata_or_protected_drift_refuses_any_write(monkeypatch, scope):
	module, state, protected, writes, transactions, _ = load_release(monkeypatch)
	snapshot = module.capture()
	if scope == "protected":
		protected["File"] = "changed"
	else:
		state[scope][-1]["unrelated"] = "changed"
	for action in (module.migrate, module.restore):
		with pytest.raises(AssertionError, match="changed"):
			action(snapshot)
	assert not writes and not transactions


def test_concurrent_description_edit_is_not_overwritten(monkeypatch):
	module, state, _, writes, transactions, _ = load_release(monkeypatch)
	snapshot = module.capture()
	state["Flow Tool"][0]["description"] = "unreviewed description"
	with pytest.raises(AssertionError, match="show_image_description_changed_since_snapshot"):
		module.migrate(snapshot)
	assert not writes and not transactions


@pytest.mark.parametrize("change", ["missing", "duplicate", "script", "wrong_import"])
def test_requires_single_existing_imported_tool(monkeypatch, change):
	module, state, _, writes, transactions, _ = load_release(monkeypatch)
	if change == "missing":
		state["Flow Tool"].pop(0)
	elif change == "duplicate":
		state["Flow Tool"].append(copy.deepcopy(state["Flow Tool"][0]))
	elif change == "script":
		state["Flow Tool"][0]["type"] = "Script"
	else:
		state["Flow Tool"][0]["import_path"] = "custom.show_image"
	with pytest.raises(AssertionError, match="show_image"):
		module.migrate(module.capture())
	assert not writes and not transactions


def image_fixture(monkeypatch):
	module, state, protected, writes, transactions, cleared = load_release(monkeypatch)
	frappe = module.frappe
	frappe.conf.db_host = "flow-check-db"
	state["File"] = [Row(
		name=f"private-file-{index}", owner=f"owner-{index}@example.invalid", file_name=f"image-{index}.png",
		file_url=f"/private/files/private-image-{index}.png", attached_to_doctype="Item",
		attached_to_name=f"private-item-{index}", attached_to_field="image", is_private=1,
	) for index in range(2)]
	def get_value(doctype, name, field):
		if doctype == "User":
			return 1
		return next(row.file_url for row in state["File"] if row.attached_to_name == name)
	frappe.db.get_value = get_value
	frappe.db.exists = lambda doctype, filters: filters["attached_to_doctype"] == "CRM Product"
	calls, behavior = [], {"wrong_item": False, "ambiguity": True, "guest_denied": True}
	media = types.ModuleType("flow.api.media")
	media._looks_like_image = lambda value: value.endswith(".png")
	def from_doc(doctype, name, field, url):
		calls.append(("document", frappe.session.user, doctype, name, field, url))
		row = next(row for row in state["File"] if row.attached_to_name == name)
		assert frappe.session.user == row.owner
		return Row(name="wrong-file" if behavior["wrong_item"] else row.name)
	def get_file(value):
		calls.append(("file", frappe.session.user, value))
		if value.startswith("/"):
			if behavior["ambiguity"]:
				raise frappe.ValidationError(module.AMBIGUOUS_IMAGE)
			return state["File"][0]
		row = next(row for row in state["File"] if row.name == value)
		if frappe.session.user == "Guest" and row.is_private and behavior["guest_denied"]:
			raise PermissionError("denied")
		return row
	media._image_file_from_doc = from_doc
	media._get_file = get_file
	api = types.ModuleType("flow.api")
	api.media = media
	monkeypatch.setitem(sys.modules, "flow.api", api)
	monkeypatch.setitem(sys.modules, "flow.api.media", media)
	return module, state, protected, writes, transactions, cleared, calls, behavior


def test_isolated_check_resolves_real_owners_and_denies_guest_without_pii(monkeypatch):
	module, state, _, writes, _, _, calls, _ = image_fixture(monkeypatch)
	# Public images permit Guest reads and cannot prove a permission denial.
	# Put one first so omitting the private-only query filter fails this test.
	state["File"].insert(0, Row(
		name="public-file", owner="public-owner@example.invalid", file_name="public-image.png",
		file_url="/files/public-image.png", attached_to_doctype="Item",
		attached_to_name="public-item", attached_to_field="image", is_private=0,
	))
	result = module._validate_image_resolution()
	assert result == {"status": "passed", "attachments_checked": 2, "document_resolution_checks": 2,
		"exact_file_checks": 2, "ambiguity_checks": 2, "permission_denials_checked": 2}
	assert len(calls) == 8
	assert "public" not in json.dumps(calls)
	assert "private" not in json.dumps(result) and "@" not in json.dumps(result)
	assert module.frappe.session.user == "Administrator"
	assert not writes


@pytest.mark.parametrize("failure", ["wrong_item", "ambiguity", "guest_denied"])
def test_isolated_check_rejects_wrong_identity_or_permission_result(monkeypatch, failure):
	module, _, _, _, _, _, _, behavior = image_fixture(monkeypatch)
	behavior[failure] = failure == "wrong_item"
	with pytest.raises(AssertionError, match="image_resolution_"):
		module._validate_image_resolution()
	assert module.frappe.session.user == "Administrator"


def test_isolated_check_requires_two_enabled_real_owner_fixtures(monkeypatch):
	module, state, _, _, _, _, _, _ = image_fixture(monkeypatch)
	state["File"][1]["owner"] = "Administrator"
	with pytest.raises(AssertionError, match="two_real_owner_duplicate_url_items_required"):
		module._validate_image_resolution()


def test_failure_is_injected_after_commit_then_old_image_restore_is_exact(monkeypatch):
	module, _, _, writes, transactions, _, _, _ = image_fixture(monkeypatch)
	snapshot = module.capture()
	with pytest.raises(RuntimeError, match="ISOLATED_INJECTED_FAILURE_AFTER_OWNER:FLOW_METADATA"):
		module.migrate(snapshot, inject_failure=True)
	assert transactions == ["commit"]
	assert module._image_tool(module.capture()["flow"])["description"] == "candidate description"
	monkeypatch.setitem(sys.modules, "flow.tools.images", None)
	monkeypatch.setitem(sys.modules, "flow.api.media", None)
	module.restore(snapshot)
	assert module.capture() == snapshot
	assert len(writes) == 2 and transactions == ["commit", "commit"]


def test_validation_failure_rolls_back_pending_description(monkeypatch):
	module, _, _, _, transactions, _, _, behavior = image_fixture(monkeypatch)
	snapshot = module.capture()
	behavior["ambiguity"] = False
	with pytest.raises(AssertionError, match="shared_url_not_ambiguous"):
		module.migrate(snapshot)
	assert transactions == ["rollback"]
	assert module._image_tool(module.capture()["flow"])["description"] == "old description"


def test_production_refuses_injection_and_skips_runtime_fixture_import(monkeypatch):
	module, _, _, writes, transactions, _ = load_release(monkeypatch)
	monkeypatch.setitem(sys.modules, "flow.api.media", None)
	with pytest.raises(AssertionError, match="failure_injection_requires_isolated_database"):
		module.migrate(module.capture(), inject_failure=True)
	assert not writes and not transactions
	assert module._validate_image_resolution() == {"status": "not_run", "reason": "isolation_only", "attachments_checked": 0}
