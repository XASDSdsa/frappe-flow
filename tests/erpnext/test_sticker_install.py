"""Managed sticker guidance updates without silently widening tool access."""

import importlib.util
import sys
import types
from pathlib import Path


class Agent(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__

	def append(self, key, value):
		self[key].append(types.SimpleNamespace(**value))

	def save(self, **kwargs):
		self.saved = True


def test_refresh_updates_bound_agents_without_enabling_or_rebinding(monkeypatch):
	agents = {
		"Flow": Agent(name="Flow", tools=[], instructions="Keep default", saved=False),
		"销售助理": Agent(name="销售助理", tools=[], instructions="Keep sales", saved=False),
		"Custom Agent": Agent(name="Custom Agent", tools=[types.SimpleNamespace(tool="sticker-tool")],
			instructions="Keep custom\n巧克粉贴纸专用工具：旧规则可新增任意型号。", saved=False),
		"Unrelated": Agent(name="Unrelated", tools=[], instructions="Keep unrelated", saved=False),
	}
	tool = {"name": "sticker-tool", "enabled": 0}
	frappe = types.ModuleType("frappe")
	frappe.db = types.SimpleNamespace(
		exists=lambda dt, name: True,
		get_value=lambda dt, filters, field: "sticker-tool" if isinstance(filters, dict) else tool[field],
		set_value=lambda dt, name, values: tool.update(values),
	)
	frappe.get_all = lambda dt, **kwargs: list(agents)
	frappe.get_doc = lambda dt, name: agents[name]
	frappe.clear_document_cache = lambda *args: None
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	flow_tools = types.ModuleType("flow.integrations.erpnext.image_guidance")
	flow_tools.SHOW_IMAGE_HINT = "current image hint"
	flow_tools.SHOW_IMAGE_HINT_EXPLICIT_ONLY = "previous image hint"
	monkeypatch.setitem(sys.modules, flow_tools.__name__, flow_tools)
	spec = importlib.util.spec_from_file_location("flow.integrations.erpnext.sticker_install", Path(__file__).resolve().parents[2] / "flow/integrations/erpnext/sticker_install.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	result = module.install_sticker_tool()
	assert result["enabled"] is False and tool["requires_confirmation"] == 1
	assert result["agents"] == ["Custom Agent"]
	assert "Keep custom" in agents["Custom Agent"].instructions
	assert "旧规则" not in agents["Custom Agent"].instructions
	assert all(model in agents["Custom Agent"].instructions for model in ("山东中性方形", "广东油性方形", "圆形"))
	assert all(not agents[name].saved and not agents[name].tools for name in ("Flow", "销售助理", "Unrelated"))

	result = module.install_sticker_tool(enable=True)
	assert result["enabled"] is True
	assert set(result["agents"]) == {"Flow", "销售助理", "Custom Agent"}
	assert all([row.tool for row in agents[name].tools] == ["sticker-tool"] for name in result["agents"])
	assert agents["Unrelated"].instructions == "Keep unrelated" and not agents["Unrelated"].saved
