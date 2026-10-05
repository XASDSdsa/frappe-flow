"""Maintain Flow-owned ERPNext tool definitions, guidance and agent bindings.

Workflow synchronization never installs an app or changes ERPNext business data,
schema or permissions. Customer prerequisites remain an explicit setup operation.
The legacy-path migration below preserves existing tool settings and identities.
"""

import json
from pathlib import Path

import frappe

LEGACY_TOOL_PATHS = json.loads(Path(__file__).with_name("legacy_tool_paths.json").read_text())
SF_WORKFLOW_MODULES = {"flow_tools", "sf_label_flow", "waybill_flow"}


def ensure_workflow_tools(enable: bool = True):
	"""Install the supported客服 workflow tools from the Flow app itself.

	The old SF app used to be the accidental installer for these registrations.
	Keeping the orchestration here makes a fresh site and an already-migrated site
	follow the same Flow-owned path. Each individual installer remains responsible
	for its own schema, guidance, bindings and idempotency.
	"""
	apps = set(frappe.get_installed_apps())
	if "erpnext" not in apps:
		return {"status": "skipped", "reason": "ERPNext 尚未安装。"}
	if not frappe.db.exists("DocType", "Flow Tool"):
		return {"status": "skipped", "reason": "Flow Tool 尚未安装。"}

	from .customer_install import install_customer_tools
	from .delivery_note_install import install_delivery_note_tools
	from .document_submission_install import install_document_submission_tools
	from .finance_flow_install import install_finance_tools
	from .inventory_install import install_inventory_tools
	from .sales_order_install import install_sales_order_tools
	from .sticker_install import install_sticker_tool

	result = {
		"customer": install_customer_tools(enable=enable, configure_prerequisites=False),
		"sticker": install_sticker_tool(enable=enable),
		"sales_order": install_sales_order_tools(enable=enable),
		"delivery_note": install_delivery_note_tools(enable=enable),
		"document_submission": install_document_submission_tools(enable=enable),
		"finance": install_finance_tools(enable=enable),
		"inventory": install_inventory_tools(enable=enable),
	}

	# SF is one shipping provider. Its Flow wrappers stay in Flow, but they are
	# only usable when the provider app that implements the API is installed.
	if "erpnext_shipping" in apps:
		from .sf_label_install import install_sf_label_tools
		from .waybill_flow_install import install_waybill_replacement_tools

		result["sf_label"] = install_sf_label_tools(enable=enable)
		result["waybill_replacement"] = install_waybill_replacement_tools(enable=enable)
	else:
		result["sf_label"] = {"status": "skipped", "reason": "Shipping 服务商应用尚未安装。"}
		result["waybill_replacement"] = {"status": "skipped", "reason": "Shipping 服务商应用尚未安装。"}

	# PayPal is optional and must not make a normal ERPNext site fail to migrate.
	if frappe.db.exists("DocType", "PayPal Receipt Record"):
		from .paypal_install import install_paypal_tool

		result["paypal"] = install_paypal_tool()
	else:
		result["paypal"] = {"status": "skipped", "reason": "PayPal 收款记录 DocType 尚未安装。"}
	return {"status": "installed", "enabled": bool(enable), "workflows": result}


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
