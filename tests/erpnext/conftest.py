"""Offline workflow tests load business modules without booting a Frappe site."""

import sys
import types
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ERP_SPEC = importlib.util.find_spec("erpnext")


@pytest.fixture(autouse=True)
def isolated_flow_packages(monkeypatch):
	# Shared helpers bind ``frappe`` at import time; never reuse one loaded under another test's stub.
	for name in [name for name in sys.modules if name.startswith("flow.integrations.erpnext.")]:
		monkeypatch.delitem(sys.modules, name)
	for name, relative in (
		("flow", "flow"),
		("flow.integrations", "flow/integrations"),
		("flow.integrations.erpnext", "flow/integrations/erpnext"),
	):
		module = types.ModuleType(name)
		module.__path__ = [str(ROOT / relative)]
		monkeypatch.setitem(sys.modules, name, module)
	if ERP_SPEC:
		erp_root = Path(ERP_SPEC.origin).parent
		for name in ("erpnext", "erpnext.stock", "erpnext.stock.doctype", "erpnext.stock.doctype.item"):
			module = types.ModuleType(name)
			module.__path__ = [str(erp_root.joinpath(*name.split(".")[1:]))]
			monkeypatch.setitem(sys.modules, name, module)
		monkeypatch.delitem(sys.modules, "erpnext.stock.doctype.item.sticker_models", raising=False)
