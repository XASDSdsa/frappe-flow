#!/usr/bin/env python3
"""Snapshot and migrate Flow-owned customer-service registrations.

This file is a release helper, not an application hook.  ``snapshot`` and
``restore`` deliberately use only Frappe's generic database APIs so they can
run from the previous image before the new Flow Python modules are available.
Only ``migrate`` and ``validate`` import the candidate Flow installer.

No customer, order, stock, accounting, address, territory, permission,
credential, session or conversation row is written by this migration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import frappe
from frappe.utils.response import json_handler


SITES = Path("/home/frappe/frappe-bench/sites")
FLOW_AGENT_TITLES = ("Flow", "销售助理")
MANAGED_TOOL_SLUGS = {
	"create_or_reuse_customer_profile",
	"preview_customer_profile",
	"create_customer_sticker_variant",
	"query_sales_order_details",
	"preview_sales_order",
	"create_sales_order_draft",
	"get_delivery_note_options",
	"preview_delivery_note",
	"create_delivery_note_draft",
	"get_purchase_receipt_options",
	"preview_purchase_receipt",
	"save_purchase_receipt",
	"get_inventory_options",
	"get_item_options",
	"get_supplier_options",
	"preview_purchase_order",
	"save_purchase_order",
	"preview_document_submission",
	"submit_reviewed_document",
	"get_sales_invoice_options",
	"preview_sales_invoice",
	"save_sales_invoice",
	"paypal_receipt_procedure",
	"prepare_sf_label",
	"list_sf_label_addresses",
	"preview_sf_label",
	"create_sf_label",
	"get_sf_label_result",
	"dispatch_sf_label",
	"preview_sf_waybill_replacement",
	"create_sf_waybill_replacement",
	"record_sf_waybill_replacement_feedback",
	"activate_sf_waybill_replacement",
}
REQUIRED_AGENT_BINDING_SLUGS = MANAGED_TOOL_SLUGS - {"paypal_receipt_procedure"}
LEGACY_DISABLED_SLUGS = {
	"book_sf_waybill",
	"print_sf_label",
	"dispatch_sf_shipment",
	"cancel_sf_waybill",
	"recreate_sf_waybill",
}
OPTIONAL_TOOL_SLUGS = {"paypal_receipt_procedure"}
SHIPPING_TOOL_SLUGS = {
	"prepare_sf_label", "list_sf_label_addresses", "preview_sf_label", "create_sf_label",
	"get_sf_label_result", "dispatch_sf_label", "preview_sf_waybill_replacement",
	"create_sf_waybill_replacement", "record_sf_waybill_replacement_feedback",
	"activate_sf_waybill_replacement",
}
TOOL_MODULES = {
	"customer_profile": {"create_or_reuse_customer_profile", "preview_customer_profile"},
	"sticker_variant": {"create_customer_sticker_variant"},
	"sales_order_flow": {"query_sales_order_details", "preview_sales_order", "create_sales_order_draft"},
	"delivery_note_flow": {"get_delivery_note_options", "preview_delivery_note", "create_delivery_note_draft"},
	"purchase_receipt_flow": {"get_purchase_receipt_options", "preview_purchase_receipt", "save_purchase_receipt"},
	"inventory_flow": {"get_inventory_options", "get_item_options", "get_supplier_options"},
	"purchase_order_flow": {"preview_purchase_order", "save_purchase_order"},
	"document_submission": {"preview_document_submission", "submit_reviewed_document"},
	"sales_invoice_flow": {"get_sales_invoice_options", "preview_sales_invoice", "save_sales_invoice"},
	"paypal_receipt": {"paypal_receipt_procedure"},
	"sf_label_flow": SHIPPING_TOOL_SLUGS - {"preview_sf_waybill_replacement", "create_sf_waybill_replacement", "record_sf_waybill_replacement_feedback", "activate_sf_waybill_replacement"},
	"waybill_flow": {"preview_sf_waybill_replacement", "create_sf_waybill_replacement", "record_sf_waybill_replacement_feedback", "activate_sf_waybill_replacement"},
}
WRITE_TOOL_SLUGS = {
	"create_or_reuse_customer_profile", "create_customer_sticker_variant", "create_sales_order_draft",
	"create_delivery_note_draft", "save_purchase_receipt", "save_sales_invoice", "paypal_receipt_procedure",
	"save_purchase_order",
	"submit_reviewed_document",
	"create_sf_label", "dispatch_sf_label", "create_sf_waybill_replacement",
	"record_sf_waybill_replacement_feedback", "activate_sf_waybill_replacement",
}

# These are protected by hashes only.  Their rows are never serialized to the
# snapshot, so release logs and snapshots cannot expose business data or
# secrets.  Missing optional DocTypes are represented explicitly.
PROTECTED_DOCTYPES = (
	"Customer", "Address", "Contact", "Territory", "Sales Order", "Sales Order Item",
	"Delivery Note", "Delivery Note Item", "Shipment", "Shipment Parcel", "Item",
	"Product Bundle", "Product Bundle Item", "Payment Entry", "Payment Entry Reference",
	"Sales Invoice", "Sales Invoice Item", "Purchase Receipt", "Purchase Receipt Item",
	"GL Entry", "Stock Ledger Entry", "Journal Entry", "Journal Entry Account",
	"Custom DocPerm", "Property Setter", "Custom Field", "DocField", "DocPerm", "User Permission", "User",
	"Flow Session", "Flow Session Message", "Flow Session Attachment", "Flow Run",
	"Flow Run Detail", "Flow Agent Memory", "File", "Comment", "Communication", "Version",
	"Flow Model", "Flow Provider", "Flow Trigger", "Role", "Has Role",
	"DocType", "Customer Group", "Warehouse", "Company", "Account", "Bin",
	"SF Waybill", "SF International Product", "PayPal Receipt Record", "Integration Request",
	"Packed Item", "Shipment Delivery Note", "Item Attribute", "Item Attribute Value",
)
PROTECTED_RAW_TABLES = ("__Auth", "tabSessions")


def _encoded(value):
	return json.dumps(value, default=json_handler, ensure_ascii=False, sort_keys=True).encode()


def _digest(value):
	return hashlib.sha256(_encoded(value)).hexdigest()


def _table_exists(doctype):
	return bool(frappe.db.table_exists(doctype))


def _rows(doctype, filters=None):
	if not _table_exists(doctype):
		return None
	return frappe.get_all(doctype, filters=filters or {}, fields=["*"], order_by="name")


def _schema_digest(doctype):
	if not _table_exists(doctype):
		return None
	return _digest(frappe.db.sql("SHOW CREATE TABLE `tab" + doctype + "`"))


def _protected_state():
	result = {}
	doctypes = set(PROTECTED_DOCTYPES)
	for doctype in PROTECTED_DOCTYPES:
		if _table_exists(doctype):
			doctypes.update(field.options for field in frappe.get_meta(doctype).get_table_fields() if field.options)
	for doctype in sorted(doctypes):
		rows = _rows(doctype)
		result[doctype] = {
			"exists": rows is not None,
			"schema": _schema_digest(doctype),
			"rows": _digest(rows) if rows is not None else None,
		}
	for table in PROTECTED_RAW_TABLES:
		exists = bool(frappe.db.sql("SHOW TABLES LIKE %s", (table,)))
		rows = frappe.db.sql("SELECT * FROM `" + table + "` ORDER BY 1", as_dict=True) if exists else None
		result[table] = {"exists": exists, "rows": _digest(rows) if rows is not None else None}
	result["Singles"] = _digest(frappe.db.sql("SELECT * FROM tabSingles ORDER BY doctype,field", as_dict=True))
	result["encryption_key"] = _digest(frappe.conf.get("encryption_key"))
	result["flow_schema"] = {scope["doctype"]: _schema_digest(scope["doctype"]) for scope in _flow_scopes()}
	return result


def _flow_scopes():
	"""Return all Flow parents and every child table row owned by each parent."""
	scopes = [{"doctype": "Flow Tool", "filters": {}}, {"doctype": "Flow Agent", "filters": {}}]
	seen = {(scope["doctype"], tuple(sorted(scope["filters"].items()))) for scope in scopes}
	if not _table_exists("Flow Agent"):
		return scopes
	for field in frappe.get_meta("Flow Agent").get_table_fields():
		if not field.options or not _table_exists(field.options):
			continue
		scope = {
			"doctype": field.options,
			"filters": {"parenttype": "Flow Agent"},
		}
		key = (scope["doctype"], tuple(sorted(scope["filters"].items())))
		if key not in seen:
			scopes.append(scope)
			seen.add(key)
	return scopes


def _capture_flow():
	return [
		{**scope, "rows": _rows(scope["doctype"], scope["filters"]) or []}
		for scope in _flow_scopes()
	]


def _semantic_flow(flow):
	groups = []
	for group in flow:
		rows = [
			{key: value for key, value in row.items() if key not in {"creation", "modified", "modified_by"}}
			for row in group["rows"]
		]
		groups.append({**group, "rows": rows})
	return _digest(groups)


def capture():
	return {"site": frappe.local.site, "flow": _capture_flow(), "protected": _protected_state()}


def _assert_protected_unchanged(snapshot):
	actual = _protected_state()
	changed = sorted(key for key in actual.keys() | snapshot["protected"].keys()
		if actual.get(key) != snapshot["protected"].get(key))
	assert not changed, "protected_business_or_security_state_changed:" + ",".join(changed)


def _clear_flow_cache():
	for doctype in ("Flow Tool", "Flow Agent"):
		for name in frappe.get_all(doctype, pluck="name"):
			frappe.clear_document_cache(doctype, name)
	frappe.clear_cache()


def _restore_flow(snapshot):
	"""Restore only Flow metadata rows captured by this helper."""
	for group in reversed(snapshot["flow"]):
		if _table_exists(group["doctype"]):
			frappe.db.delete(group["doctype"], group["filters"])
	for group in snapshot["flow"]:
		rows = group["rows"]
		if not rows:
			continue
		fields = list(rows[0])
		frappe.db.bulk_insert(group["doctype"], fields, [tuple(row.get(field) for field in fields) for row in rows])
	frappe.db.commit()
	_clear_flow_cache()
	assert _encoded(capture()) == _encoded(snapshot), "flow_metadata_restore_not_exact"


def _managed_rows():
	rows = frappe.get_all(
		"Flow Tool",
		filters={"slug": ["in", sorted(MANAGED_TOOL_SLUGS | LEGACY_DISABLED_SLUGS)]},
		fields=["name", "slug", "type", "enabled", "requires_confirmation", "import_path", "code"],
	)
	assert len({row.slug for row in rows}) == len(rows), "duplicate_managed_flow_tool_slug"
	return {row.slug: row for row in rows}


def _validate_sales_order_query():
	"""Exercise the candidate read tool as a real sales user, only in isolation.

	The result deliberately contains no user, order, customer or item identity.
	No usable existing fixture means missing evidence, not a passing release.
	"""
	if not frappe.conf.db_host.startswith(("flow-check-", "shipment-check-")):
		return {"status": "not_run", "reason": "isolation_only", "orders_checked": 0}
	query = frappe.get_attr("flow.integrations.erpnext.sales_order_flow.query_sales_order_details")
	assert not frappe.flags.get("ignore_permissions"), "sales_order_query_unverified:permissions_bypassed"
	sales_users = set(frappe.get_all("Has Role", filters={
		"parenttype": "User", "role": ["in", ["Sales User", "Sales Manager"]],
	}, pluck="parent")) - {"Administrator", "Guest"}
	users = sorted(frappe.get_all("User", filters={
		"name": ["in", sorted(sales_users)], "enabled": 1, "user_type": "System User",
	}, pluck="name")) if sales_users else []
	assert users, "sales_order_query_unverified:no_enabled_sales_user"
	original_user = frappe.session.user
	try:
		for user in users:
			frappe.set_user(user)
			try:
				visible = frappe.get_list("Sales Order", filters={"docstatus": ["!=", 2]},
					fields=["name"], order_by="creation desc, name desc", limit_page_length=1)
			except frappe.PermissionError:
				continue
			if visible:
				break
		else:
			raise AssertionError("sales_order_query_unverified:no_visible_sales_order")
		order = frappe.get_doc("Sales Order", visible[0]["name"])
		order.check_permission("read")
		expected = order.get("items") or []
		assert expected, "sales_order_query_unverified:no_item_rows"
		result = query()
		assert result.get("status") == "found" and result.get("verified") is True, "sales_order_query_failed"
		assert result.get("sales_order") == order.name, "sales_order_query_wrong_visible_order"
		items = result.get("items")
		assert isinstance(items, list) and len(items) == len(expected), "sales_order_query_incomplete_items"
		for actual, native in zip(items, expected, strict=True):
			assert actual.get("sales_order_item") == native.name and actual.get("row_no") == native.idx, "sales_order_query_item_identity_changed"
			for field in ("item_code", "item_name", "uom"):
				assert actual.get(field) == native.get(field), "sales_order_query_item_field_changed:" + field
			for field in ("qty", "rate", "amount", "delivered_qty", "billed_amt"):
				assert actual.get(field) == float(native.get(field) or 0), "sales_order_query_item_number_changed:" + field
			for field in ("description", "warehouse", "delivery_date"):
				assert actual.get(field) == str(native.get(field) or ""), "sales_order_query_item_text_changed:" + field
			assert actual.get("is_free_item") == bool(native.get("is_free_item")), "sales_order_query_free_item_changed"
		frappe.set_user("Guest")
		assert not frappe.has_permission("Sales Order", ptype="read", doc=order), "sales_order_query_unverified:no_denied_permission_fixture"
		denied = query(sales_order=order.name)
		assert denied.get("status") == "error" and denied.get("verified") is False and denied.get("items") == [], "sales_order_query_permission_bypass"
		assert not denied.get("sales_order"), "sales_order_query_denied_data_exposed"
		return {"status": "passed", "orders_checked": 1, "items_checked": len(expected), "permission_denials_checked": 1}
	finally:
		frappe.set_user(original_user)


def _validate_contract(snapshot):
	_assert_protected_unchanged(snapshot)
	rows = _managed_rows()
	optional = set(OPTIONAL_TOOL_SLUGS)
	if "erpnext_shipping" not in set(frappe.get_installed_apps()):
		optional |= SHIPPING_TOOL_SLUGS
	missing = sorted(MANAGED_TOOL_SLUGS - optional - set(rows))
	assert not missing, "managed_flow_tools_missing:" + ",".join(missing)
	for slug in MANAGED_TOOL_SLUGS - optional | (set(rows) & optional):
		row = rows[slug]
		assert row.type == "Imported" and not row.code, "flow_tool_is_script:" + slug
		module = next(module for module, slugs in TOOL_MODULES.items() if slug in slugs)
		assert row.import_path == "flow.integrations.erpnext." + module + "." + slug, "flow_tool_wrong_import:" + slug
		assert callable(frappe.get_attr(row.import_path)), "flow_tool_unresolved:" + slug
		assert int(row.requires_confirmation or 0) == int(slug in WRITE_TOOL_SLUGS), "flow_tool_confirmation_wrong:" + slug
		if slug != "paypal_receipt_procedure":
			assert int(row.enabled or 0) == 1, "flow_tool_disabled:" + slug
	for slug in LEGACY_DISABLED_SLUGS:
		row = rows.get(slug)
		if row:
			assert int(row.enabled or 0) == 0, "legacy_flow_tool_enabled:" + slug
	for title in FLOW_AGENT_TITLES:
		name = frappe.db.get_value("Flow Agent", {"title": title}, "name")
		assert name, "flow_agent_missing:" + title
		bound = set(frappe.get_all("Flow Agent Tool", filters={"parent": name, "parenttype": "Flow Agent"}, pluck="tool"))
		required = REQUIRED_AGENT_BINDING_SLUGS - optional
		missing = sorted(rows[slug].name for slug in required if rows[slug].name not in bound)
		assert not missing, "flow_agent_tool_unbound:" + title
		legacy = {rows[slug].name for slug in LEGACY_DISABLED_SLUGS if slug in rows}
		assert not legacy & bound, "legacy_flow_tool_bound:" + title
	_validate_preserved_metadata(snapshot)
	query_check = _validate_sales_order_query()
	_assert_protected_unchanged(snapshot)
	return {"tools": sorted(MANAGED_TOOL_SLUGS), "agents": list(FLOW_AGENT_TITLES), "semantic": _semantic_flow(_capture_flow()), "sales_order_query": query_check}


def _validate_preserved_metadata(snapshot):
	"""Protect custom tools, agent settings, knowledge bindings and custom guidance."""
	from flow.integrations.erpnext import customer_install, delivery_note_install, finance_flow_install
	from flow.integrations.erpnext import document_submission_install, inventory_install
	from flow.integrations.erpnext import sales_order_install, sf_label_install, sticker_install, waybill_flow_install

	before = {group["doctype"]: group["rows"] for group in snapshot["flow"]}
	after = {group["doctype"]: group["rows"] for group in _capture_flow()}
	managed = MANAGED_TOOL_SLUGS | LEGACY_DISABLED_SLUGS
	assert {row["slug"] for row in after["Flow Tool"]} <= {row["slug"] for row in before["Flow Tool"]} | managed, "unexpected_flow_tool_added"
	for row in before["Flow Tool"]:
		if row["slug"] not in managed:
			actual = next((item for item in after["Flow Tool"] if item["name"] == row["name"]), None)
			assert _encoded(actual) == _encoded(row), "unmanaged_flow_tool_changed:" + row["name"]
	assert {row["name"] for row in before["Flow Agent"]} == {row["name"] for row in after["Flow Agent"]}, "flow_agent_inventory_changed"
	tool_slugs = {row["name"]: row["slug"] for row in before["Flow Tool"]}
	after_slugs = {row["name"]: row["slug"] for row in after["Flow Tool"]}
	before_bindings = before.get("Flow Agent Tool", [])
	after_bindings = after.get("Flow Agent Tool", [])
	shipping_installed = "erpnext_shipping" in set(frappe.get_installed_apps())

	def binding_rows(rows, parent):
		return [row for row in rows if row.get("parent") == parent and row.get("parenttype") == "Flow Agent"]

	def binding_semantics(row):
		# Frappe may refresh these fields while saving the parent or reorder child
		# rows after appending a managed tool. All business binding fields remain
		# protected and are compared exactly.
		return {key: value for key, value in row.items() if key not in {"creation", "modified", "modified_by", "idx"}}

	for original in before["Flow Agent"]:
		actual = next(row for row in after["Flow Agent"] if row["name"] == original["name"])
		fixed = set(original) - {"instructions", "modified", "modified_by"}
		assert _encoded({key: actual.get(key) for key in fixed}) == _encoded({key: original[key] for key in fixed}), "flow_agent_settings_changed:" + original["name"]
		original_rows = binding_rows(before_bindings, original["name"])
		actual_rows = binding_rows(after_bindings, original["name"])
		bound = {tool_slugs.get(row["tool"]) for row in original_rows}
		target = original["title"] in FLOW_AGENT_TITLES
		actual_by_name = {row.get("name"): row for row in actual_rows}
		for row in original_rows:
			slug = tool_slugs.get(row.get("tool"))
			if shipping_installed and slug in LEGACY_DISABLED_SLUGS:
				continue
			restored = actual_by_name.get(row.get("name"))
			assert restored is not None, "flow_agent_binding_removed:" + original["name"] + ":" + str(row.get("name"))
			assert binding_semantics(restored) == binding_semantics(row), "flow_agent_binding_changed:" + original["name"] + ":" + str(row.get("name"))
		added = [row for row in actual_rows if row.get("name") not in {item.get("name") for item in original_rows}]
		for row in added:
			slug = after_slugs.get(row.get("tool"))
			if target:
				assert slug in MANAGED_TOOL_SLUGS, "target_agent_unmanaged_binding_added:" + original["name"]
			else:
				assert False, "non_target_agent_binding_added:" + original["name"]
		expected = original.get("instructions") or ""
		if target or bound & {"create_or_reuse_customer_profile", "preview_customer_profile"}:
			expected = customer_install.with_customer_guidance(expected)
		if target or "create_customer_sticker_variant" in bound:
			expected = sticker_install.with_sticker_guidance(expected)
		if target:
			# Match ensure_workflow_tools' installation order, including the
			# reviewed-submission and inventory workflows installed on every site.
			for transform in (
				sales_order_install.with_sales_order_guidance,
				delivery_note_install.with_delivery_note_guidance,
				document_submission_install.with_document_submission_guidance,
				finance_flow_install.with_finance_guidance,
				inventory_install.with_inventory_guidance,
			):
				expected = transform(expected)
			if "erpnext_shipping" in set(frappe.get_installed_apps()):
				for transform in (sf_label_install.with_sf_label_guidance, waybill_flow_install.with_replacement_guidance):
					expected = transform(expected)
		assert actual.get("instructions") == expected, "flow_agent_guidance_changed_outside_contract:" + original["name"]
		actual_bound = [after_slugs.get(row["tool"]) for row in after.get("Flow Agent Tool", []) if row["parent"] == original["name"]]
		assert len(actual_bound) == len(set(actual_bound)), "duplicate_flow_agent_binding:" + original["name"]
		expected_bound = bound - (LEGACY_DISABLED_SLUGS if "erpnext_shipping" in set(frappe.get_installed_apps()) else set())
		if target:
			expected_bound |= REQUIRED_AGENT_BINDING_SLUGS - (set() if shipping_installed else SHIPPING_TOOL_SLUGS)
		elif bound & {"create_or_reuse_customer_profile", "preview_customer_profile"}:
			# Existing nonstandard agents retain their own tool selection. Customer
			# guidance refresh does not bind an additional customer tool.
			pass
		assert set(actual_bound) == expected_bound, "flow_agent_bindings_changed_outside_contract:" + original["name"]
	for doctype in before.keys() - {"Flow Tool", "Flow Agent", "Flow Agent Tool"}:
		assert _encoded(before[doctype]) == _encoded(after[doctype]), "flow_agent_child_changed:" + doctype


def _install_workflows():
	from flow.integrations.erpnext.install import ensure_workflow_tools

	return ensure_workflow_tools(enable=True)


def migrate(snapshot, inject_failure=False):
	assert snapshot["site"] == frappe.local.site
	if inject_failure:
		assert frappe.conf.db_host.startswith(("flow-check-", "shipment-check-")), "failure_injection_requires_isolated_database"
	_assert_protected_unchanged(snapshot)
	try:
		result = _install_workflows()
		contract = _validate_contract(snapshot)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	if inject_failure:
		# Keep the release runner's existing sentinel. The isolated rehearsal now
		# has persisted metadata to exercise the actual old-image restore path.
		raise RuntimeError("ISOLATED_INJECTED_FAILURE_AFTER_OWNER:FLOW_METADATA")
	return {"workflows": result, "contract": contract}


def validate(snapshot):
	assert snapshot["site"] == frappe.local.site
	return _validate_contract(snapshot)


def restore(snapshot):
	assert snapshot["site"] == frappe.local.site
	_assert_protected_unchanged(snapshot)
	_restore_flow(snapshot)
	print("EXACT_FLOW_METADATA_ROLLBACK_OK")


def compare(snapshot):
	assert _encoded(capture()) == _encoded(snapshot), "flow_metadata_snapshot_differs"
	print("FLOW_METADATA_SNAPSHOT_EXACT_MATCH")


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("mode", choices=("snapshot", "migrate", "validate", "restore", "compare", "fail-after-owner"))
	parser.add_argument("--site", required=True)
	parser.add_argument("--snapshot", required=True, type=Path)
	parser.add_argument("--db-host", required=True)
	args = parser.parse_args()
	if args.mode == "fail-after-owner":
		assert args.db_host.startswith(("flow-check-", "shipment-check-")), "failure_injection_requires_isolated_database"
	args.snapshot = args.snapshot.resolve()
	os.chdir(SITES)
	for directory in (SITES.parent / "logs", SITES / args.site / "logs"):
		assert directory.is_dir() and os.access(directory, os.W_OK), "missing_or_unwritable_log_directory"
	frappe.init(args.site, sites_path=str(SITES))
	assert frappe.conf.db_host == args.db_host, "unexpected_database_host"
	if args.db_host.startswith(("flow-check-", "shipment-check-")):
		prefix = "flow-check-" if args.db_host.startswith("flow-check-") else "shipment-check-"
		assert all((urlparse(frappe.conf.get(key) or "").hostname or "").startswith(prefix) for key in ("redis_cache", "redis_queue", "redis_socketio")), "unexpected_isolation_redis_host"
	frappe.connect(); frappe.set_user("Administrator")
	try:
		if args.mode == "snapshot":
			assert not args.snapshot.exists(), "snapshot_exists"
			temporary = args.snapshot.with_suffix(".pending")
			temporary.write_bytes(_encoded(capture())); temporary.chmod(0o600); os.replace(temporary, args.snapshot)
			print("FLOW_METADATA_SNAPSHOT " + hashlib.sha256(args.snapshot.read_bytes()).hexdigest())
		elif args.mode in {"migrate", "fail-after-owner"}:
			snapshot = json.loads(args.snapshot.read_text())
			before = _semantic_flow(_capture_flow())
			result = migrate(snapshot, inject_failure=args.mode == "fail-after-owner")
			print(json.dumps({"result": "MIGRATION_OK", "before": before, "after": _semantic_flow(_capture_flow()), "actions": result}, default=json_handler))
		elif args.mode == "validate":
			print(json.dumps(validate(json.loads(args.snapshot.read_text())), default=json_handler))
		elif args.mode == "restore":
			restore(json.loads(args.snapshot.read_text()))
		else:
			compare(json.loads(args.snapshot.read_text()))
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main()
