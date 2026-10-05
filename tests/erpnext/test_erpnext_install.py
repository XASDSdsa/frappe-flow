"""Contracts for the Flow-owned ERPNext workflow installer."""

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_install(monkeypatch, apps=None, flow_tool=True, paypal=False):
	frappe = types.ModuleType("frappe")
	frappe.get_installed_apps = lambda: list(apps or ("erpnext", "erpnext_shipping"))
	frappe.db = types.SimpleNamespace(
		exists=lambda doctype, name=None: (
			flow_tool if name == "Flow Tool" else paypal if name == "PayPal Receipt Record" else True
		)
	)
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	path = ROOT / "flow/integrations/erpnext/install.py"
	spec = importlib.util.spec_from_file_location("flow.integrations.erpnext.install_under_test", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def test_installer_owns_all_customer_facing_workflows(monkeypatch):
	module = load_install(monkeypatch, paypal=True)
	called = []

	def fake(name):
		def install(**kwargs):
			called.append((name, kwargs["enable"]))
			return {"tool": name}

		return install

	modules = {
		"flow.integrations.erpnext.customer_install": {"install_customer_tools": fake("customer")},
		"flow.integrations.erpnext.sticker_install": {"install_sticker_tool": fake("sticker")},
		"flow.integrations.erpnext.sales_order_install": {
			"install_sales_order_tools": fake("sales_order"),
		},
		"flow.integrations.erpnext.delivery_note_install": {"install_delivery_note_tools": fake("delivery_note")},
		"flow.integrations.erpnext.document_submission_install": {"install_document_submission_tools": fake("document_submission")},
		"flow.integrations.erpnext.finance_flow_install": {"install_finance_tools": fake("finance")},
		"flow.integrations.erpnext.inventory_install": {"install_inventory_tools": fake("inventory")},
		"flow.integrations.erpnext.sf_label_install": {
			"install_sf_label_tools": fake("sf_label"),
			"install_sf_query_tools": fake("sf_query"),
		},
		"flow.integrations.erpnext.waybill_flow_install": {"install_waybill_replacement_tools": fake("waybill")},
		"flow.integrations.erpnext.paypal_install": {"install_paypal_tool": lambda: called.append(("paypal", True)) or {"tool": "paypal"}},
		"flow.integrations.erpnext.tool_install": {
			"apply_agent_guidance": lambda guidance: called.append(("routing", True)) or ["Flow"],
		},
	}
	for name, values in modules.items():
		stub = types.ModuleType(name)
		for attr, value in values.items():
			setattr(stub, attr, value)
		monkeypatch.setitem(sys.modules, name, stub)

	result = module.ensure_workflow_tools(enable=True)

	assert result["status"] == "installed"
	assert [name for name, _ in called] == [
		"customer", "sticker", "sales_order", "delivery_note",
		"document_submission", "finance", "inventory", "sf_label", "sf_query", "waybill", "paypal", "routing",
	]
	assert all(enabled for _, enabled in called)


def test_provider_workflows_are_skipped_without_shipping_app(monkeypatch):
	module = load_install(monkeypatch, apps=("erpnext",))
	for module_name, functions in {
		"flow.integrations.erpnext.customer_install": {"install_customer_tools": lambda **_: {}},
		"flow.integrations.erpnext.sticker_install": {"install_sticker_tool": lambda **_: {}},
		"flow.integrations.erpnext.sales_order_install": {
			"install_sales_order_tools": lambda **_: {},
		},
		"flow.integrations.erpnext.delivery_note_install": {"install_delivery_note_tools": lambda **_: {}},
		"flow.integrations.erpnext.document_submission_install": {"install_document_submission_tools": lambda **_: {}},
		"flow.integrations.erpnext.finance_flow_install": {"install_finance_tools": lambda **_: {}},
		"flow.integrations.erpnext.inventory_install": {"install_inventory_tools": lambda **_: {}},
		"flow.integrations.erpnext.tool_install": {"apply_agent_guidance": lambda guidance: []},
	}.items():
		stub = types.ModuleType(module_name)
		for name, function in functions.items():
			setattr(stub, name, function)
		monkeypatch.setitem(sys.modules, module_name, stub)
	result = module.ensure_workflow_tools(enable=True)

	assert result["workflows"]["sf_label"]["status"] == "skipped"
	assert result["workflows"]["sf_query"]["status"] == "skipped"
	assert result["workflows"]["waybill_replacement"]["status"] == "skipped"
