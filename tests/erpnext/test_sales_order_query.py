"""Root-regression tests for the Flow-owned Sales Order read tool."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]


class Doc:
	def __init__(self, **values):
		self.__dict__.update(values)

	def get(self, key, default=None):
		return getattr(self, key, default)

	def check_permission(self, _permission):
		return None


def load_query_tool(monkeypatch):
	frappe = types.ModuleType("frappe")
	frappe.get_list = Mock(return_value=[{"name": "SAL-ORD-2026-00018"}])
	frappe.get_doc = Mock()
	frappe.PermissionError = PermissionError
	utils = types.ModuleType("frappe.utils")
	utils.flt = lambda value: float(value or 0)
	utils.nowdate = lambda: "2026-09-30"
	utils.now_datetime = lambda: "2026-09-30 12:00:00"
	utils.strip_html = lambda value: str(value)
	frappe.utils = utils
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)

	resolution = types.ModuleType("flow.integrations.erpnext.sales_order_resolution")
	resolution.STANDALONE_STICKER_WARNING = ""
	resolution.resolve_order_inputs = Mock()
	monkeypatch.setitem(sys.modules, "flow.integrations.erpnext.sales_order_resolution", resolution)
	tool_module = types.ModuleType("flow.lib.tool")
	tool_module.tool = lambda function, **_options: function
	monkeypatch.setitem(sys.modules, "flow.lib.tool", tool_module)

	spec = importlib.util.spec_from_file_location(
		"flow.integrations.erpnext.sales_order_flow_query_test",
		ROOT / "flow/integrations/erpnext/sales_order_flow.py",
	)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module, frappe


def test_latest_order_query_reads_complete_items_once(monkeypatch):
	module, frappe = load_query_tool(monkeypatch)
	order = Doc(
		name="SAL-ORD-2026-00018",
		customer="Driven25",
		customer_name="Driven25",
		company="莱亚国际",
		currency="USD",
		transaction_date="2026-09-30",
		delivery_date="2026-10-07",
		status="To Deliver",
		docstatus=0,
		grand_total=1200,
		advance_paid=1200,
		items=[
			Doc(
				idx=1, name="row-1", item_code="GREEN", item_name="山东中性绿方",
				description="绿方", qty=120, uom="颗", rate=3, amount=360,
				warehouse="大坪仓库 - LEYA", delivery_date="2026-10-07",
				delivered_qty=0, billed_amt=0, is_free_item=0,
			),
		],
	)
	frappe.get_doc.return_value = order

	result = module.query_sales_order_details()

	assert result["status"] == "found"
	assert result["verified"] is True
	assert result["sales_order"] == "SAL-ORD-2026-00018"
	assert result["advance_paid"] == 1200
	assert result["items"] == [{
		"row_no": 1, "sales_order_item": "row-1", "item_code": "GREEN",
		"item_name": "山东中性绿方", "description": "绿方", "qty": 120.0,
		"uom": "颗", "rate": 3.0, "amount": 360.0,
		"warehouse": "大坪仓库 - LEYA", "delivery_date": "2026-10-07",
		"delivered_qty": 0.0, "billed_amt": 0.0, "is_free_item": False,
	}]
	frappe.get_list.assert_called_once()
	assert frappe.get_list.call_args.kwargs["order_by"] == "creation desc, name desc"
	assert frappe.get_list.call_args.kwargs["limit_page_length"] == 1


def test_unknown_latest_order_returns_clear_result(monkeypatch):
	module, frappe = load_query_tool(monkeypatch)
	frappe.get_list.return_value = []

	result = module.query_sales_order_details()

	assert result == {
		"status": "not_found",
		"verified": True,
		"message": "当前账号没有可读取的有效销售订单。",
		"items": [],
	}
	frappe.get_doc.assert_not_called()
