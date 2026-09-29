"""Move existing business-tool registrations without changing their behavior.

This optional integration never installs ERPNext/Shipping, creates business data,
changes agent instructions, or enables a tool. Fresh setup remains an explicit
operation through the individual workflow installers.
"""

import json
from pathlib import Path

import frappe

LEGACY_TOOL_PATHS = json.loads(Path(__file__).with_name("legacy_tool_paths.json").read_text())
SF_WORKFLOW_MODULES = {"flow_tools", "sf_label_flow", "waybill_flow"}


def migrate_legacy_tool_paths():
	"""Change only known imported-tool paths and preserve historical identities."""
	apps = set(frappe.get_installed_apps())
	if "erpnext" not in apps:
		return {"changed": [], "skipped": "ERPNext is not installed"}
	if not frappe.db.exists("DocType", "Flow Tool"):
		return {"changed": [], "skipped": "Flow Tool is not installed"}
	rows = frappe.get_all(
		"Flow Tool",
		filters={"import_path": ["in", list(LEGACY_TOOL_PATHS)]},
		fields=["name", "type", "import_path"],
	)
	# Validate the entire set before writing the first row. A failed migration may
	# not leave only half the tools pointing at the new release.
	for row in rows:
		if row.type != "Imported":
			frappe.throw(f"Tool {row.name} has a legacy import path but is not an Imported tool.")
		module = row.import_path.rsplit(".", 2)[-2]
		if module in SF_WORKFLOW_MODULES and "erpnext_shipping" not in apps:
			frappe.throw("ERPNext Shipping must be installed before migrating SF International tools.")
		frappe.get_attr(LEGACY_TOOL_PATHS[row.import_path])
	changed = []
	for row in rows:
		frappe.db.set_value(
			"Flow Tool", row.name, "import_path", LEGACY_TOOL_PATHS[row.import_path], update_modified=False
		)
		frappe.clear_document_cache("Flow Tool", row.name)
		changed.append(row.name)
	return {"changed": changed}
