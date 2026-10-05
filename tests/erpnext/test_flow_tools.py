"""Flow tool contract tests for asynchronous SF waybill booking."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


class Document:
	def __init__(self, **values):
		self.__dict__.update(values)
		self.permissions = []

	def get(self, key, default=None):
		return getattr(self, key, default)

	def check_permission(self, permission):
		self.permissions.append(permission)

	def reload(self):
		return self


def load_flow_tools(monkeypatch):
	frappe = types.ModuleType("frappe")
	frappe._ = lambda value: value
	frappe.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(ValueError(message))
	frappe.whitelist = lambda **_kwargs: (lambda function: function)
	utils = types.ModuleType("frappe.utils")
	utils.cint = lambda value: int(value or 0)
	utils.flt = lambda value: float(value or 0)
	utils.get_url = lambda: "https://erp.example.com"
	frappe.utils = utils
	tool_module = types.ModuleType("flow.lib.tool")
	tool_module.tool = lambda func=None, **_kwargs: (func if func is not None else (lambda wrapped: wrapped))
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)
	monkeypatch.setitem(sys.modules, "flow", types.ModuleType("flow"))
	monkeypatch.setitem(sys.modules, "flow.lib", types.ModuleType("flow.lib"))
	monkeypatch.setitem(sys.modules, "flow.lib.tool", tool_module)
	spec = importlib.util.spec_from_file_location(
		"flow_tools_under_test", ROOT / "flow/integrations/erpnext/flow_tools.py"
	)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return frappe, module


def shipping_stub(doc, *, enabled=True, after_save=None):
	shipping = types.SimpleNamespace()
	shipping.is_enabled = lambda: enabled
	shipping._sf_waybill = lambda current: str(current.get("shipment_id") or current.get("awb_number") or "").strip()
	def save():
		if after_save:
			after_save(doc)
	doc.save = save
	return shipping


@pytest.mark.parametrize("waybill,status", [("SF123", "Booked"), ("", "创建中"), ("", "失败")])
def test_retired_booking_never_writes_or_calls_carrier(monkeypatch, waybill, status):
	frappe, flow_tools = load_flow_tools(monkeypatch)
	def forbidden(*args, **kwargs):
		raise AssertionError("Retired tool must not load/mutate a shipment or call SF")
	frappe.get_doc = forbidden
	flow_tools._shipping = forbidden
	result = flow_tools.book_sf_waybill("SHIP-1")
	assert result["ok"] is False and result["verified"] is False
	assert result["status"] == "retired"
	assert "本次未下单" in result["message"]
	assert flow_tools.book_sf_waybill not in flow_tools.FLOW_TOOL_OBJECTS
	assert flow_tools.print_sf_label not in flow_tools.FLOW_TOOL_OBJECTS
	assert flow_tools.dispatch_sf_shipment not in flow_tools.FLOW_TOOL_OBJECTS
	assert flow_tools.cancel_sf_waybill not in flow_tools.FLOW_TOOL_OBJECTS
	assert flow_tools.recreate_sf_waybill not in flow_tools.FLOW_TOOL_OBJECTS
