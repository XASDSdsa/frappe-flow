#!/usr/bin/env python3
"""Refresh only the existing show_image description and verify attachment identity.

The shared snapshot helper is loaded from the running Flow app. Snapshot,
restore and compare therefore work in the previous image without importing the
candidate image tools. No tool installer, document save or image preview runs.
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
TOOL_IMPORTS = {"flow.tools.builtins.show_image", "flow.tools.images.show_image"}
AMBIGUOUS_IMAGE = (
	"Multiple image attachments match. Specify the document and image field, or an exact File ID."
)


@cache
def _library():
	path = Path(frappe.get_app_path("flow")).parent / "deploy/customer_service_workflows/metadata.py"
	spec = importlib.util.spec_from_file_location("flow_image_metadata_snapshot_library", path)
	assert spec and spec.loader, "flow_snapshot_library_unavailable"
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def capture():
	return _library().capture()


def _image_tool(flow):
	groups = [group for group in flow if group["doctype"] == "Flow Tool" and not group["filters"]]
	assert len(groups) == 1, "flow_tool_snapshot_scope_invalid"
	rows = [row for row in groups[0]["rows"] if row.get("slug") == "show_image"]
	assert len(rows) == 1, "existing_show_image_tool_required"
	row = rows[0]
	assert row.get("name") and "description" in row, "show_image_identity_or_description_missing"
	assert row.get("type") == "Imported" and row.get("import_path") in TOOL_IMPORTS, "show_image_import_invalid"
	return row


def _description():
	from flow.tools.images import show_image

	assert isinstance(show_image.description, str) and show_image.description.strip(), "show_image_description_empty"
	return show_image.description


def _assert_only_description_changed(snapshot):
	assert snapshot["site"] == frappe.local.site, "snapshot_site_mismatch"
	library = _library()
	library._assert_protected_unchanged(snapshot)
	original = _image_tool(snapshot["flow"])
	actual = library._capture_flow()
	current = _image_tool(actual)
	description = current["description"]
	current["description"] = original["description"]
	assert library._encoded(actual) == library._encoded(snapshot["flow"]), "flow_metadata_changed_outside_image_description"
	return description


def _assert_expected(snapshot, description):
	assert _assert_only_description_changed(snapshot) == description, "show_image_description_mismatch"


def _image_fixtures(media):
	"""Find real duplicate-URL attachments without returning their identities."""
	fixtures = []
	items = set()
	for row in frappe.get_all(
		"File", filters={"attached_to_doctype": "Item", "attached_to_field": "image", "is_private": 1},
		fields=["name", "owner", "file_url", "file_name", "attached_to_name"], order_by="name",
	):
		if row.owner in {"Administrator", "Guest", None, ""} or row.attached_to_name in items:
			continue
		if not media._looks_like_image(row.file_name or row.file_url):
			continue
		if frappe.db.get_value("Item", row.attached_to_name, "image") != row.file_url:
			continue
		if not frappe.db.exists("File", {"file_url": row.file_url, "attached_to_doctype": "CRM Product"}):
			continue
		if not frappe.db.get_value("User", row.owner, "enabled"):
			continue
		fixtures.append(row)
		items.add(row.attached_to_name)
		if len(fixtures) == 2:
			break
	assert len(fixtures) == 2, "image_resolution_unverified:two_real_owner_duplicate_url_items_required"
	return fixtures


def _validate_image_resolution():
	if not frappe.conf.db_host.startswith(ISOLATED_PREFIXES):
		return {"status": "not_run", "reason": "isolation_only", "attachments_checked": 0}
	assert not frappe.flags.get("ignore_permissions"), "image_resolution_unverified:permissions_bypassed"
	from flow.api import media

	fixtures = _image_fixtures(media)
	original_user = frappe.session.user
	try:
		for row in fixtures:
			frappe.set_user(row.owner)
			resolved = media._image_file_from_doc("Item", row.attached_to_name, "image", row.file_url)
			assert resolved.name == row.name, "image_resolution_wrong_item_attachment"
			assert media._get_file(row.name).name == row.name, "image_resolution_wrong_exact_attachment"
			try:
				media._get_file(row.file_url)
			except frappe.ValidationError as error:
				assert str(error) == frappe._(AMBIGUOUS_IMAGE), "image_resolution_wrong_ambiguity_error"
			else:
				raise AssertionError("image_resolution_shared_url_not_ambiguous")
			frappe.set_user("Guest")
			try:
				media._get_file(row.name)
			except frappe.PermissionError:
				pass
			else:
				raise AssertionError("image_resolution_guest_permission_bypass")
	finally:
		frappe.set_user(original_user)
	return {
		"status": "passed", "attachments_checked": len(fixtures),
		"document_resolution_checks": len(fixtures), "exact_file_checks": len(fixtures),
		"ambiguity_checks": len(fixtures), "permission_denials_checked": len(fixtures),
	}


def _validate_contract(snapshot, description):
	_assert_expected(snapshot, description)
	image_check = _validate_image_resolution()
	_assert_expected(snapshot, description)
	return {"semantic": _library()._semantic_flow(_library()._capture_flow()), "image_resolution": image_check}


def migrate(snapshot, inject_failure=False):
	if inject_failure:
		assert frappe.conf.db_host.startswith(ISOLATED_PREFIXES), "failure_injection_requires_isolated_database"
	description = _description()
	current = _assert_only_description_changed(snapshot)
	original = _image_tool(snapshot["flow"])
	assert current in (original["description"], description), "show_image_description_changed_since_snapshot"
	changed = current != description
	try:
		if changed:
			frappe.db.set_value("Flow Tool", original["name"], "description", description, update_modified=False)
		contract = _validate_contract(snapshot, description)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	frappe.clear_document_cache("Flow Tool", original["name"])
	if inject_failure:
		raise RuntimeError("ISOLATED_INJECTED_FAILURE_AFTER_OWNER:FLOW_METADATA")
	return {"descriptions_updated": int(changed), "contract": contract}


def validate(snapshot):
	return _validate_contract(snapshot, _description())


def restore(snapshot):
	current = _assert_only_description_changed(snapshot)
	original = _image_tool(snapshot["flow"])
	try:
		if current != original["description"]:
			frappe.db.set_value(
				"Flow Tool", original["name"], "description", original["description"], update_modified=False,
			)
		_assert_expected(snapshot, original["description"])
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	frappe.clear_document_cache("Flow Tool", original["name"])
	compare(snapshot)
	print("EXACT_FLOW_METADATA_ROLLBACK_OK")


def compare(snapshot):
	assert _library()._encoded(capture()) == _library()._encoded(snapshot), "flow_metadata_snapshot_differs"
	print("FLOW_METADATA_SNAPSHOT_EXACT_MATCH")


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
	os.chdir(SITES)
	for directory in (SITES.parent / "logs", SITES / args.site / "logs"):
		assert directory.is_dir() and os.access(directory, os.W_OK), "missing_or_unwritable_log_directory"
	frappe.init(args.site, sites_path=str(SITES))
	assert frappe.conf.db_host == args.db_host, "unexpected_database_host"
	if args.db_host.startswith(ISOLATED_PREFIXES):
		prefix = next(prefix for prefix in ISOLATED_PREFIXES if args.db_host.startswith(prefix))
		assert all(
			(urlparse(frappe.conf.get(key) or "").hostname or "").startswith(prefix)
			for key in ("redis_cache", "redis_queue", "redis_socketio")
		), "unexpected_isolation_redis_host"
	frappe.connect()
	frappe.set_user("Administrator")
	try:
		library = _library()
		if args.mode == "snapshot":
			assert not args.snapshot.exists(), "snapshot_exists"
			snapshot = capture()
			_image_tool(snapshot["flow"])
			temporary = args.snapshot.with_suffix(".pending")
			with temporary.open("xb") as handle:
				os.chmod(temporary, 0o600)
				handle.write(library._encoded(snapshot))
			os.replace(temporary, args.snapshot)
			print("FLOW_METADATA_SNAPSHOT " + hashlib.sha256(args.snapshot.read_bytes()).hexdigest())
		elif args.mode in {"migrate", "fail-after-owner"}:
			snapshot = json.loads(args.snapshot.read_text())
			before = library._semantic_flow(library._capture_flow())
			result = migrate(snapshot, inject_failure=args.mode == "fail-after-owner")
			print(json.dumps({
				"result": "MIGRATION_OK", "before": before,
				"after": library._semantic_flow(library._capture_flow()), "actions": result,
			}))
		elif args.mode == "validate":
			print(json.dumps(validate(json.loads(args.snapshot.read_text()))))
		elif args.mode == "restore":
			restore(json.loads(args.snapshot.read_text()))
		else:
			compare(json.loads(args.snapshot.read_text()))
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main()
