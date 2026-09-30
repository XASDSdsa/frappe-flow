"""Offline contracts for Flow's model prompt compatibility layer."""

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_compat(monkeypatch):
	frappe = types.ModuleType("frappe")
	frappe._ = lambda value: value
	frappe.whitelist = lambda **_kwargs: lambda function: function
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	spec = importlib.util.spec_from_file_location("flow_compat_under_test", ROOT / "flow/compat.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def test_tool_descriptions_are_compacted_without_mutating_input(monkeypatch):
	module = load_compat(monkeypatch)
	tools = [{"function": {"name": "lookup", "description": "x" * 300}}]
	result = module._compact_tools(tools)
	assert len(result[0]["function"]["description"]) <= 161
	assert len(tools[0]["function"]["description"]) == 300


def test_unpaired_tool_calls_receive_a_safe_stub(monkeypatch):
	module = load_compat(monkeypatch)
	messages = [{"role": "assistant", "tool_calls": [{"id": "call-1"}]}]
	result = module._fill_missing_tool_outputs(messages)
	assert result[-1]["role"] == "tool"
	assert result[-1]["tool_call_id"] == "call-1"


def test_sales_order_detail_result_is_not_truncated(monkeypatch):
	module = load_compat(monkeypatch)
	content = "{" + "\"items\":[" + ("{\"row_no\":1}," * 220) + "]}"
	messages = [
		{
			"role": "assistant",
			"tool_calls": [{"id": "order-1", "function": {"name": "query_sales_order_details"}}],
		},
		{"role": "tool", "tool_call_id": "order-1", "content": content},
	]

	result = module._compact_tool_history(messages)

	assert result[-1]["content"] == content
