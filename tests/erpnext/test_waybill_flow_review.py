import importlib.util
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
MODULE = "flow.integrations.erpnext.waybill_flow"


class Doc(dict):
	__getattr__ = dict.get

	def __setattr__(self, key, value):
		self[key] = value

	def check_permission(self, *_args, **_kwargs):
		return None


def load_module(monkeypatch):
	cache = {}
	frappe = types.ModuleType("frappe")
	frappe.session = SimpleNamespace(user="sales@example.com")
	frappe.flags = SimpleNamespace(flow_run="RUN-1")
	frappe.local = SimpleNamespace(site="test.local")
	frappe.PermissionError = PermissionError
	frappe.DuplicateEntryError = KeyError
	frappe.throw = lambda message: (_ for _ in ()).throw(ValueError(message))
	frappe.cache = SimpleNamespace(
		get_value=lambda key: cache.get(key),
		set_value=lambda key, value, **_kwargs: cache.__setitem__(key, value),
	)
	run = SimpleNamespace(owner="sales@example.com", session="FLOW-SESSION")
	frappe.db = SimpleNamespace(
		get_value=lambda doctype, name, fields=None, as_dict=False: run if doctype == "Flow Run" and as_dict else (1 if doctype == "User" else None),
		exists=lambda *_args: False,
	)
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	utils = types.ModuleType("frappe.utils")
	utils.now_datetime = lambda: datetime(2026, 9, 23, 12)
	utils.get_datetime = datetime.fromisoformat
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)
	tool_module = types.ModuleType("flow.lib.tool")
	tool_module.tool = lambda fn=None, **kwargs: (fn if fn is not None else (lambda wrapped: wrapped))
	monkeypatch.setitem(sys.modules, "flow.lib.tool", tool_module)
	sales = types.ModuleType("flow.integrations.erpnext.sales_order_flow")
	sales._actor = lambda: frappe.session.user
	sales._scope = lambda: run.session
	monkeypatch.setitem(sys.modules, sales.__name__, sales)
	waybill = types.ModuleType("erpnext_shipping.sf_international.waybill")
	waybill.ACTIVE = "当前"
	waybill._waybill_doctype_available = lambda: True
	waybill._waybill = lambda doc: doc.get("shipment_id") or ""
	waybill._shipment_is_shipped = lambda _doc: False
	waybill._cancel_evidence_source = lambda _doc, record: {"waybill": record.get("waybill")} if record.get("carrier_cancelled") else None
	waybill._records = lambda _doc: [Doc(name="WB-OLD", waybill="OLD-WB", is_active=0, replacement_status="已取消", carrier_cancelled=1)]
	monkeypatch.setitem(sys.modules, waybill.__name__, waybill)
	package = types.ModuleType("erpnext_shipping.sf_international")
	package.__path__ = []
	package.waybill = waybill
	monkeypatch.setitem(sys.modules, package.__name__, package)
	spec = importlib.util.spec_from_file_location(MODULE, ROOT / "flow/integrations/erpnext/waybill_flow.py")
	module = importlib.util.module_from_spec(spec)
	sys.modules[MODULE] = module
	# get_doc is installed after import so tests can tailor it per assertion.
	spec.loader.exec_module(module)
	return module, frappe, waybill, cache


def test_scope_uses_flow_run_session(monkeypatch):
	module, _frappe, _waybill, _cache = load_module(monkeypatch)
	assert module._scope() == "FLOW-SESSION"


def test_cancelled_old_waybill_with_exact_evidence_is_readable(monkeypatch):
	module, frappe, waybill, _cache = load_module(monkeypatch)
	doc = Doc(name="SHIP-1", shipment_id="OLD-WB", sf_form_json="{}")
	record = Doc(name="WB-OLD", shipment="SHIP-1", waybill="OLD-WB", is_active=0,
		replacement_status="已取消", carrier_cancelled=1)
	frappe.get_doc = lambda doctype, name=None, **_kwargs: doc if doctype == "Shipment" else record
	got_doc, got_record, shipped = module._read_state("SHIP-1")
	assert got_doc is doc and got_record.name == "WB-OLD" and shipped is False


def test_execution_ledger_replays_without_second_carrier_call(monkeypatch):
	module, frappe, waybill, cache = load_module(monkeypatch)
	ledger = Doc(name="flow-sf-waybill-replacement-" + "a" * 32,
		integration_request_service=module.SERVICE,
		status="Completed",
		data=json.dumps({"user": "sales@example.com", "scope": "FLOW-SESSION", "site": "test.local"}),
		output=json.dumps({"status": "失败", "verified": False, "record": "WB-NEW"}))
	frappe.db.exists = lambda doctype, name: doctype == "Integration Request"
	frappe.get_doc = lambda doctype, name=None, **_kwargs: ledger
	assert module._existing_execution("a" * 32)["idempotent"] is True
	assert module._existing_execution("a" * 32)["record"] == "WB-NEW"
