"""Small shared primitives for Flow Imported Tool installers."""

import frappe

AGENT_TITLES = ("Flow", "销售助理")


def upsert_imported_tools(definitions, *, enable=False):
	"""Create or refresh Imported tools; ``enable=False`` keeps each row's enabled state."""
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
			"summary": definition.get("summary") or definition["title"],
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


def all_enabled(tool_names):
	return all(bool(frappe.db.get_value("Flow Tool", name, "enabled")) for name in tool_names)


def find_agent_names(agent_titles=AGENT_TITLES):
	"""Resolve agents by title first, then by an identical document name."""
	if not frappe.db.exists("DocType", "Flow Agent"):
		return []
	names = []
	for title in agent_titles:
		name = frappe.db.get_value("Flow Agent", {"title": title}, "name") or (
			title if frappe.db.exists("Flow Agent", title) else None
		)
		if name and name not in names:
			names.append(name)
	return names


def _refresh_agent(name, *, tool_names=(), guidance=None):
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


def bind_imported_tools(tool_names, *, enable=False, agent_titles=AGENT_TITLES, guidance=None):
	"""Bind tools to named agents and optionally refresh their managed guidance."""
	if not enable:
		return []
	agents = find_agent_names(agent_titles)
	for name in agents:
		_refresh_agent(name, tool_names=tool_names, guidance=guidance)
	return agents


def apply_agent_guidance(guidance, *, agent_titles=AGENT_TITLES):
	"""Refresh one managed guidance block on the named business agents."""
	agents = find_agent_names(agent_titles)
	for name in agents:
		_refresh_agent(name, guidance=guidance)
	return agents
