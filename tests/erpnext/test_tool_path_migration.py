"""Registration migration preserves access, approvals and historical tool identities."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "flow/integrations/erpnext/install.py"
PATHS = json.loads(SOURCE.with_name("legacy_tool_paths.json").read_text())


def harness(monkeypatch, apps=("flow", "erpnext", "erpnext_shipping"), rows=None):
	rows = rows if rows is not None else []
	f = SimpleNamespace(
		get_installed_apps=lambda: apps,
		db=SimpleNamespace(exists=lambda *args: True),
		get_attr=Mock(return_value=lambda: None),
		clear_document_cache=Mock(),
	)
	def get_all(doctype, filters, fields):
		return [SimpleNamespace(**{key: row[key] for key in fields}) for row in rows
			if row["import_path"] in filters["import_path"][1]]
	def set_value(doctype, name, field, value, update_modified):
		assert doctype == "Flow Tool" and field == "import_path" and update_modified is False
		next(row for row in rows if row["name"] == name)[field] = value
	def throw(message):
		raise ValueError(message)
	f.get_all = get_all
	f.db.set_value = Mock(side_effect=set_value)
	f.throw = throw
	monkeypatch.setitem(sys.modules, "frappe", f)
	spec = importlib.util.spec_from_file_location("tool_path_migration_test", SOURCE)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module, f


def row(path, **kwargs):
	return {"name": path.rsplit(".", 1)[1], "import_path": path, "type": "Imported",
		"enabled": 1, "requires_confirmation": 1, "modified": "unchanged", **kwargs}


def test_pure_flow_site_never_imports_optional_business_apps(monkeypatch):
	m, f = harness(monkeypatch, apps=("flow",))
	assert m.migrate_legacy_tool_paths() == {"changed": [], "skipped": "ERPNext is not installed"}
	f.get_attr.assert_not_called()
	f.db.set_value.assert_not_called()


def test_all_34_paths_preserve_access_approval_and_identity(monkeypatch):
	rows = [row(path, enabled=int(index % 2 == 0), requires_confirmation=index % 2)
		for index, path in enumerate(PATHS)]
	before = deepcopy(rows)
	m, f = harness(monkeypatch, rows=rows)
	assert len(PATHS) == 34
	assert len(m.migrate_legacy_tool_paths()["changed"]) == 34
	for original, migrated in zip(before, rows):
		assert migrated == {**original, "import_path": PATHS[original["import_path"]]}
	assert m.migrate_legacy_tool_paths() == {"changed": []}
	assert f.db.set_value.call_count == 34


def test_unknown_custom_tool_is_not_rewritten(monkeypatch):
	rows = [row("custom_app.workflow.execute")]
	m, f = harness(monkeypatch, rows=rows)
	assert m.migrate_legacy_tool_paths() == {"changed": []}
	f.db.set_value.assert_not_called()


def test_missing_shipping_prevents_partial_migration(monkeypatch):
	rows = [row("sf_international.sf_international.customer_profile.preview_customer_profile"),
		row("sf_international.sf_international.flow_tools.query_sf_tracking")]
	m, f = harness(monkeypatch, apps=("flow", "erpnext"), rows=rows)
	with pytest.raises(ValueError, match="Shipping"):
		m.migrate_legacy_tool_paths()
	f.db.set_value.assert_not_called()


def test_bad_import_or_unexpected_script_prevents_all_writes(monkeypatch):
	rows = [row(path) for path in list(PATHS)[:2]]
	m, f = harness(monkeypatch, rows=rows)
	f.get_attr.side_effect = [lambda: None, ImportError("missing implementation")]
	with pytest.raises(ImportError, match="missing implementation"):
		m.migrate_legacy_tool_paths()
	f.db.set_value.assert_not_called()
	f.get_attr.side_effect = None
	rows[1]["type"] = "Script"
	with pytest.raises(ValueError, match="not an Imported"):
		m.migrate_legacy_tool_paths()
	f.db.set_value.assert_not_called()


def test_procurement_uses_native_stock_ownership():
	import ast

	tree = ast.parse(SOURCE.with_name("purchase_receipt_flow.py").read_text())
	imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
	assert any(node.module == "erpnext.stock.doctype.item.sticker_stock"
		and {alias.name for alias in node.names} == {"is_customer_sticker", "sticker_customer"}
		for node in imports)
