"""Focused tests for the read-only inventory selectors."""
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]


def load_module(monkeypatch, rows=None):
    class InputError(Exception):
        def __init__(self, message, fields=None):
            super().__init__(message)
            self.fields = fields or []

    frappe = types.ModuleType("frappe")
    frappe.get_list = lambda *args, **kwargs: list(rows or [])
    frappe.get_doc = lambda *args, **kwargs: SimpleNamespace(check_permission=lambda *_: None)
    reviewed = types.ModuleType("flow.integrations.erpnext.reviewed_flow")
    reviewed.InputError = InputError
    monkeypatch.setitem(sys.modules, "frappe", frappe)
    monkeypatch.setitem(sys.modules, "flow", types.ModuleType("flow"))
    monkeypatch.setitem(sys.modules, "flow.integrations", types.ModuleType("flow.integrations"))
    monkeypatch.setitem(sys.modules, "flow.integrations.erpnext", types.ModuleType("flow.integrations.erpnext"))
    monkeypatch.setitem(sys.modules, "flow.integrations.erpnext.reviewed_flow", reviewed)
    path = ROOT / "flow/integrations/erpnext/inventory_flow.py"
    spec = importlib.util.spec_from_file_location("flow.integrations.erpnext.inventory_flow", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, InputError


def test_inventory_separates_actual_reserved_and_projected(monkeypatch):
    row = SimpleNamespace(item_code="STICKER", warehouse="WH", actual_qty=10,
        reserved_qty=3, ordered_qty=8, indented_qty=0, planned_qty=0,
        projected_qty=15, reserved_stock=3, stock_uom="Nos", company="Co",
        valuation_rate=2.5, stock_value=25)
    module, _ = load_module(monkeypatch, [row])

    result = module.get_inventory_options(item_code="STICKER", warehouse="WH")

    assert result["verified"] is True
    assert result["items"][0]["actual_qty"] == 10
    assert result["items"][0]["reserved_qty"] == 3
    assert result["items"][0]["available_qty"] == 7
    assert result["items"][0]["projected_qty"] == 15


def test_inventory_rejects_ambiguous_item_filters(monkeypatch):
    module, InputError = load_module(monkeypatch)

    result = module.get_inventory_options(item_code="A", item_query="A")

    assert result["status"] == "needs_input"
    assert "item_code" in result["missing"]
    assert InputError
