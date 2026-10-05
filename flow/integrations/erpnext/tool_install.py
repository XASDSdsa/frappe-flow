"""Small shared primitives for Flow Imported Tool installers."""

import frappe


def upsert_imported_tools(definitions, *, enable=False):
	"""Create or refresh Imported tools without changing existing disabled state."""
	if not frappe.db.exists("DocType", "Flow Tool"):
		frappe.throw("Flow 尚未安装")

	installed = []
	for definition in definitions:
		values = {
			"type": "Imported",
			"code": None,
			"title": definition["title"],
			"import_path": definition["import_path"],
			"requires_confirmation": int(definition["requires_confirmation"]),
			"description": definition["description"],
			"summary": definition["summary"],
		}
		if enable:
			values["enabled"] = 1

		name = frappe.db.get_value("Flow Tool", {"slug": definition["slug"]}, "name")
		if name:
			frappe.db.set_value("Flow Tool", name, values)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Flow Tool",
					"slug": definition["slug"],
					"enabled": int(enable),
					**values,
				}
			)
			doc.insert(ignore_permissions=True)
			name = doc.name
		frappe.clear_document_cache("Flow Tool", name)
		installed.append(name)
	return installed


def bind_imported_tools(tool_names, *, enable=False, agent_titles=(), guidance=None):
	"""Bind tools to named agents and optionally refresh their managed guidance."""
	if not enable or not frappe.db.exists("DocType", "Flow Agent"):
		return []

	agents = []
	for title in agent_titles:
		name = frappe.db.get_value("Flow Agent", {"title": title}, "name") or (
			title if frappe.db.exists("Flow Agent", title) else None
		)
		if not name or name in agents:
			continue
		agent = frappe.get_doc("Flow Agent", name)
		changed = False
		for tool_name in tool_names:
			if not any(row.tool == tool_name for row in agent.get("tools") or []):
				agent.append("tools", {"tool": tool_name})
				changed = True
		if guidance:
			instructions = guidance(agent.get("instructions"))
			if instructions != (agent.get("instructions") or ""):
				agent.instructions = instructions
				changed = True
		if changed:
			agent.save(ignore_permissions=True, ignore_version=True)
		frappe.clear_document_cache("Flow Agent", name)
		agents.append(name)
	return agents
