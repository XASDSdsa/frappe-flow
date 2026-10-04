#!/usr/bin/env python3
"""Refresh managed Markdown text without running installers or saving documents.

The private plan beside the snapshot records exact before/after text. Restore
uses that plan and generic Frappe APIs, so it also works in the previous image.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from functools import cache
from pathlib import Path
from urllib.parse import urlparse

import frappe


SITES = Path("/home/frappe/frappe-bench/sites")
ISOLATED_PREFIXES = ("flow-check-", "shipment-check-")
FIELDS = {"Flow Tool": "description", "Flow Agent": "instructions"}


@cache
def _library():
	path = Path(frappe.get_app_path("flow")).parent / "deploy/customer_service_workflows/metadata.py"
	spec = importlib.util.spec_from_file_location("flow_markdown_snapshot_library", path)
	assert spec and spec.loader, "flow_snapshot_library_unavailable"
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def capture():
	return _library().capture()


def _rows(flow, doctype):
	groups = [group for group in flow if group["doctype"] == doctype and not group["filters"]]
	assert len(groups) == 1, "flow_snapshot_scope_invalid:" + doctype
	return groups[0]["rows"]


def _row(flow, doctype, name):
	rows = [row for row in _rows(flow, doctype) if row.get("name") == name]
	assert len(rows) == 1, "flow_metadata_identity_invalid:" + doctype
	return rows[0]


def prepare(snapshot):
	"""Build a pure text plan from the candidate definitions and original rows."""
	from flow.integrations.erpnext.markdown_guidance import description_definitions, refresh_existing_guidance

	assert snapshot["site"] == frappe.local.site, "snapshot_site_mismatch"
	changes, slugs, missing = [], set(), 0
	for definition in description_definitions():
		slug = definition["slug"]
		assert slug not in slugs, "duplicate_source_tool_slug:" + slug
		slugs.add(slug)
		rows = [row for row in _rows(snapshot["flow"], "Flow Tool") if row.get("slug") == slug]
		assert len(rows) <= 1, "duplicate_managed_tool_slug:" + slug
		if not rows:
			missing += 1
			continue  # Existing registrations only; optional tools are never created.
		row = rows[0]
		assert row.get("type") == "Imported" and row.get("import_path") == definition["import_path"], "managed_tool_import_invalid:" + slug
		assert isinstance(definition["description"], str) and definition["description"].strip(), "managed_tool_description_empty:" + slug
		if row.get("description") != definition["description"]:
			changes.append({"doctype": "Flow Tool", "name": row["name"], "field": "description",
				"before": row.get("description"), "after": definition["description"]})
	for row in _rows(snapshot["flow"], "Flow Agent"):
		if not row.get("instructions"):
			continue
		instructions = refresh_existing_guidance(row["instructions"])
		assert isinstance(instructions, str), "managed_guidance_invalid"
		if instructions != row["instructions"]:
			changes.append({"doctype": "Flow Agent", "name": row["name"], "field": "instructions",
				"before": row["instructions"], "after": instructions})
	return {"site": snapshot["site"], "snapshot_sha256": _library()._digest(snapshot), "changes": changes,
		"missing_tools_skipped": missing}


def _check_plan(snapshot, plan):
	assert snapshot["site"] == plan["site"] == frappe.local.site, "snapshot_site_mismatch"
	assert plan["snapshot_sha256"] == _library()._digest(snapshot), "markdown_plan_snapshot_mismatch"
	seen = set()
	for change in plan["changes"]:
		doctype, name, field = change["doctype"], change["name"], change["field"]
		assert FIELDS.get(doctype) == field, "markdown_plan_field_invalid"
		assert (doctype, name) not in seen, "duplicate_markdown_plan_target"
		seen.add((doctype, name))
		original = _row(snapshot["flow"], doctype, name)
		assert field in original and original[field] == change["before"], "markdown_plan_original_mismatch"
		assert isinstance(change["after"], str) and change["after"] != change["before"], "markdown_plan_text_invalid"


def _assert_state(snapshot, plan, target=None):
	_check_plan(snapshot, plan)
	library = _library()
	library._assert_protected_unchanged(snapshot)
	actual = library._capture_flow()
	for change in plan["changes"]:
		row = _row(actual, change["doctype"], change["name"])
		value = row[change["field"]]
		assert value in (change["before"], change["after"]), "managed_text_changed_since_snapshot"
		if target:
			assert value == change[target], "managed_text_does_not_match_plan"
		row[change["field"]] = change["before"]
	assert library._encoded(actual) == library._encoded(snapshot["flow"]), "flow_metadata_changed_outside_markdown_plan"


def _apply(snapshot, plan, target):
	_check_plan(snapshot, plan)
	updated = {"Flow Tool": 0, "Flow Agent": 0}
	try:
		# Locking reads see current values even under MariaDB repeatable read.
		current = []
		for change in plan["changes"]:
			rows = frappe.db.sql(
				f"SELECT `{change['field']}` FROM `tab{change['doctype']}` WHERE name=%s FOR UPDATE",
				(change["name"],),
			)
			assert len(rows) == 1, "flow_metadata_target_missing"
			value = rows[0][0]
			assert value in (change["before"], change["after"]), "managed_text_changed_since_snapshot"
			current.append(value)
		_assert_state(snapshot, plan)
		for change, value in zip(plan["changes"], current):
			if value != change[target]:
				frappe.db.set_value(change["doctype"], change["name"], change["field"], change[target], update_modified=False)
				updated[change["doctype"]] += 1
		_assert_state(snapshot, plan, target)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	for change in plan["changes"]:
		frappe.clear_document_cache(change["doctype"], change["name"])
	return {"descriptions_updated": updated["Flow Tool"], "instructions_updated": updated["Flow Agent"]}


def migrate(snapshot, plan, inject_failure=False):
	if inject_failure:
		assert frappe.conf.db_host.startswith(ISOLATED_PREFIXES), "failure_injection_requires_isolated_database"
	assert plan == prepare(snapshot), "markdown_plan_candidate_mismatch"
	result = _apply(snapshot, plan, "after")
	if inject_failure:
		raise RuntimeError("ISOLATED_INJECTED_FAILURE_AFTER_OWNER:FLOW_METADATA")
	return {**result, "missing_tools_skipped": plan["missing_tools_skipped"]}


def validate(snapshot, plan):
	assert plan == prepare(snapshot), "markdown_plan_candidate_mismatch"
	_assert_state(snapshot, plan, "after")
	return {"status": "verified", "planned_fields": len(plan["changes"])}


def restore(snapshot, plan):
	_apply(snapshot, plan, "before")
	compare(snapshot)
	print("EXACT_FLOW_METADATA_ROLLBACK_OK")


def compare(snapshot):
	assert _library()._encoded(capture()) == _library()._encoded(snapshot), "flow_metadata_snapshot_differs"
	print("FLOW_METADATA_SNAPSHOT_EXACT_MATCH")


def _save_private(path, value):
	assert not path.exists(), "metadata_file_exists"
	temporary = path.with_name(path.name + ".pending")
	with temporary.open("xb") as handle:
		os.chmod(temporary, 0o600)
		handle.write(_library()._encoded(value))
	os.replace(temporary, path)


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("mode", choices=("snapshot", "migrate", "validate", "restore", "compare", "fail-after-owner"))
	parser.add_argument("--site", required=True)
	parser.add_argument("--snapshot", required=True, type=Path)
	parser.add_argument("--db-host", required=True)
	args = parser.parse_args()
	if args.mode == "fail-after-owner":
		assert args.db_host.startswith(ISOLATED_PREFIXES), "failure_injection_requires_isolated_database"
	args.snapshot = args.snapshot.resolve()
	plan_path = args.snapshot.with_name(args.snapshot.name + ".markdown-plan.json")
	os.chdir(SITES)
	for directory in (SITES.parent / "logs", SITES / args.site / "logs"):
		assert directory.is_dir() and os.access(directory, os.W_OK), "missing_or_unwritable_log_directory"
	frappe.init(args.site, sites_path=str(SITES))
	assert frappe.conf.db_host == args.db_host, "unexpected_database_host"
	if args.db_host.startswith(ISOLATED_PREFIXES):
		prefix = next(prefix for prefix in ISOLATED_PREFIXES if args.db_host.startswith(prefix))
		assert all((urlparse(frappe.conf.get(key) or "").hostname or "").startswith(prefix)
			for key in ("redis_cache", "redis_queue", "redis_socketio")), "unexpected_isolation_redis_host"
	frappe.connect()
	frappe.set_user("Administrator")
	try:
		if args.mode == "snapshot":
			assert not plan_path.exists(), "markdown_plan_exists"
			_save_private(args.snapshot, capture())
			print("FLOW_METADATA_SNAPSHOT " + hashlib.sha256(args.snapshot.read_bytes()).hexdigest())
			return
		snapshot = json.loads(args.snapshot.read_text())
		if args.mode == "compare":
			compare(snapshot)
			return
		if args.mode in {"migrate", "fail-after-owner"} and not plan_path.exists():
			plan = prepare(snapshot)
			_assert_state(snapshot, plan, "before")
			_save_private(plan_path, plan)
		plan = json.loads(plan_path.read_text())
		if args.mode in {"migrate", "fail-after-owner"}:
			before = _library()._digest(_library()._capture_flow())
			result = migrate(snapshot, plan, inject_failure=args.mode == "fail-after-owner")
			after = _library()._digest(_library()._capture_flow())
			print(json.dumps({"result": "MIGRATION_OK", "before": before, "after": after, "actions": result}))
		elif args.mode == "validate":
			print(json.dumps(validate(snapshot, plan)))
		else:
			restore(snapshot, plan)
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main()
