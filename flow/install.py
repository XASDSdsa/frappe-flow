"""Installation and migration helpers for generic Flow settings."""

from __future__ import annotations

import frappe


def after_migrate() -> None:
	"""Keep Flow-owned settings and workspace links in the Flow module."""
	if "erpnext" in frappe.get_installed_apps() and frappe.db.exists("DocType", "Flow Tool"):
		# Keep every客服 workflow reproducible from the Flow repository. This also
		# migrates the historical database-only order-details Script to the
		# permission-aware implementation and binds all current tools to the
		# supported business agents.
		from flow.integrations.erpnext.install import ensure_workflow_tools

		ensure_workflow_tools(enable=True)
	if frappe.db.exists("DocType", "Flow Voice Settings"):
		module = frappe.db.get_value("DocType", "Flow Voice Settings", "module")
		if module != "Flow":
			frappe.db.set_value("DocType", "Flow Voice Settings", "module", "Flow", update_modified=False)
	if not frappe.db.exists("Workspace", "Flow") or not frappe.db.exists("DocType", "Flow Voice Settings"):
		return
	workspace = frappe.get_doc("Workspace", "Flow")
	for link in workspace.links or []:
		if link.link_to == "Flow Voice Settings" or link.label == "Flow 语音设置":
			link.link_type = "DocType"
			link.link_to = "Flow Voice Settings"
			link.label = "Flow 语音设置"
			workspace.save(ignore_permissions=True)
			break
	else:
		workspace.append("links", {"link_type": "DocType", "link_to": "Flow Voice Settings", "label": "Flow 语音设置"})
		workspace.save(ignore_permissions=True)

	if frappe.db.exists("Workspace Sidebar", "Flow"):
		sidebar = frappe.get_doc("Workspace Sidebar", "Flow")
		for item in sidebar.items or []:
			if item.link_to == "Flow Voice Settings" or item.label == "Flow 语音设置":
				item.link_type = "DocType"
				item.link_to = "Flow Voice Settings"
				item.label = "Flow 语音设置"
				break
		else:
			sidebar.append(
				"items",
				{
					"type": "Link",
					"label": "Flow 语音设置",
					"link_type": "DocType",
					"link_to": "Flow Voice Settings",
					"child": 1,
				},
			)
		sidebar.save(ignore_permissions=True)
