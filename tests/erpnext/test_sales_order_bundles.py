"""Native bundle identity, ownership, permissions and reuse boundaries."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest


class Doc(dict):
	__getattr__ = dict.get

	def check_permission(self, action):
		self.setdefault("checks", []).append(action)
		if action in self.get("denied", []):
			raise PermissionError("restricted")


class NativeDoc(Doc):
	def insert(self, **kwargs):
		assert not kwargs, "Native permission checks may not be bypassed."
		self.check_permission("create")
		self["name"] = self.get("item_code") if self.doctype == "Item" else self.get("new_item_code")
		if self.name in self.harness.data[self.doctype]:
			raise self.harness.frappe.DuplicateEntryError(self.name)
		self.harness.inserts.append((self.doctype, self.name))
		self.harness.data[self.doctype][self.name] = self
		return self


class Harness:
	def __init__(self, monkeypatch):
		self.inserts, self.denied_create, self.list_queries = [], set(), []
		self.data = {
			"Customer": {"CUST": Doc(name="CUST", customer_name="Driven25")},
			"Product Bundle": {},
			"Item": {
				"CHALK": Doc(name="CHALK", item_name="山东绿方", is_stock_item=1, is_sales_item=1,
					item_group="Products", stock_uom="Nos", sales_uom="Box", grant_commission=1,
					uoms=[Doc(uom="Nos", conversion_factor=1), Doc(uom="Box", conversion_factor=10)],
					item_defaults=[Doc(company="Company", income_account="Sales", selling_cost_center="Main",
						default_price_list="Private list", default_supplier="Private supplier", expense_account="Expense")],
					taxes=[Doc(item_tax_template="Tax", minimum_net_rate=0, maximum_net_rate=100)], image="/files/chalk.png"),
				"STICKER": Doc(name="STICKER", item_name="Driven25绿方v1", is_stock_item=1, is_sales_item=1,
					stock_uom="Sheet", variant_of="巧克粉贴纸", attributes=[
						Doc(attribute="客户", attribute_value="Driven25"), Doc(attribute="贴纸型号", attribute_value="绿方"),
						Doc(attribute="贴纸版本", attribute_value="v1")]),
			},
		}
		self.frappe = types.ModuleType("frappe")
		self.frappe.PermissionError = PermissionError
		self.frappe.DoesNotExistError = KeyError
		self.frappe.DuplicateEntryError = type("DuplicateEntryError", (Exception,), {})
		self.frappe.get_doc = lambda dt, name: self.data[dt][name]
		self.frappe.get_all = self.get_all
		self.frappe.get_list = self.get_list
		self.frappe.new_doc = self.new_doc
		self.frappe.clear_document_cache = Mock()
		self.frappe.db = types.SimpleNamespace(exists=self.exists, commit=Mock(), rollback=Mock())
		monkeypatch.setitem(sys.modules, "frappe", self.frappe)
		path = Path(__file__).resolve().parents[2] / "flow/integrations/erpnext/sales_order_bundles.py"
		spec = importlib.util.spec_from_file_location("sales_order_bundles_under_test", path)
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)

	def exists(self, doctype, key):
		if isinstance(key, str):
			return key in self.data[doctype]
		return any(all(row.get(k, 0) == v for k, v in key.items()) for row in self.data[doctype].values())

	def get_all(self, doctype, filters, **kwargs):
		assert doctype == "Customer", "Bundle candidates must use permission-aware get_list."
		return [row for row in self.data[doctype].values() if all(row.get(k) == v for k, v in filters.items())]

	def get_list(self, doctype, filters, **kwargs):
		self.list_queries.append((doctype, filters))
		return [row for row in self.data[doctype].values() if "read" not in row.get("denied", [])]

	def new_doc(self, doctype):
		return NativeDoc(doctype=doctype, harness=self, denied=["create"] if doctype in self.denied_create else [])

	def ensure(self, **kwargs):
		return self.module.ensure_bundle("CHALK", "STICKER", "CUST", **kwargs)

	def manual(self, name="MANUAL"):
		profile = self.module._profile(self.data["Item"]["CHALK"])
		self.data["Item"][name] = Doc(name=name, item_name="Existing customer combo", is_stock_item=0,
			is_sales_item=1, **deepcopy(profile))
		self.data["Product Bundle"][name] = Doc(name=name, new_item_code=name, items=[
			Doc(item_code="CHALK", qty=1, uom="Nos"), Doc(item_code="STICKER", qty=1, uom="Sheet")])
		return self.data["Item"][name], self.data["Product Bundle"][name]


@pytest.fixture
def h(monkeypatch):
	return Harness(monkeypatch)


def test_new_bundle_uses_separate_stock_components_and_preserves_sales_units(h):
	result = h.ensure()
	parent = h.data["Item"][result["item_code"]]
	assert result["disposition"] == "create"
	assert len(result["signature"]) == 64
	assert [(r["item_code"], r["qty"], r["uom"]) for r in result["components"]] == [
		("CHALK", 1, "Nos"), ("STICKER", 1, "Sheet")]
	assert parent.stock_uom == "Nos" and parent.sales_uom == "Box"
	assert parent.uoms == [{"uom": "Box", "conversion_factor": 10}, {"uom": "Nos", "conversion_factor": 1}]
	assert parent.is_stock_item == parent.is_purchase_item == parent.include_item_in_manufacturing == 0
	assert parent.item_defaults == [{"company": "Company", "income_account": "Sales", "selling_cost_center": "Main"}]
	assert "Private" not in repr(parent.item_defaults)
	assert parent.taxes == h.data["Item"]["CHALK"].taxes
	assert h.data["Item"]["CHALK"].checks == ["read"]
	assert h.data["Item"]["STICKER"].checks == ["read"]
	h.frappe.db.commit.assert_not_called()
	h.frappe.db.rollback.assert_not_called()


def test_repeated_call_reuses_exact_generated_bundle_without_writes(h):
	first = h.ensure()
	second = h.ensure(planned_code=first["item_code"])
	assert second["disposition"] == "reuse"
	assert second["signature"] == first["signature"]
	assert len(h.inserts) == 2


def test_exact_unique_manual_bundle_is_reused(h):
	h.manual()
	result = h.ensure()
	assert result["item_code"] == "MANUAL" and result["disposition"] == "reuse"
	assert not h.inserts
	assert h.list_queries[0][0] == "Product Bundle"


@pytest.mark.parametrize("change", ["qty", "uom", "extra", "stock", "disabled", "accounts", "conversion"])
def test_changed_reviewed_bundle_is_refused_without_overwriting(h, change):
	parent, bundle = h.manual()
	if change == "qty":
		bundle["items"][1]["qty"] = 2
	elif change == "uom":
		bundle["items"][1]["uom"] = "Box"
	elif change == "extra":
		bundle["items"].append(Doc(item_code="STICKER", qty=1, uom="Sheet"))
	elif change == "stock":
		parent["is_stock_item"] = 1
	elif change == "disabled":
		bundle["disabled"] = 1
	elif change == "accounts":
		parent.item_defaults[0]["income_account"] = "Other"
	else:
		parent.uoms[0]["conversion_factor"] = 20
	with pytest.raises(h.module.BundleInputError, match="不匹配"):
		h.ensure(planned_code="MANUAL")
	assert not h.inserts


def test_multiple_exact_manual_matches_require_resolution(h):
	h.manual("A")
	h.manual("B")
	with pytest.raises(h.module.BundleInputError, match="多个"):
		h.ensure()
	assert not h.inserts


@pytest.mark.parametrize("kind", ["other_customer", "duplicate_customer", "missing_version", "not_stock"])
def test_sticker_ownership_and_stock_requirements_cannot_be_bypassed(h, kind):
	sticker = h.data["Item"]["STICKER"]
	if kind == "other_customer":
		sticker.attributes[0]["attribute_value"] = "Another customer"
	elif kind == "duplicate_customer":
		h.data["Customer"]["OTHER"] = Doc(name="OTHER", customer_name="Driven25")
	elif kind == "missing_version":
		sticker.attributes.pop()
	else:
		sticker["is_stock_item"] = 0
	with pytest.raises(h.module.BundleInputError):
		h.ensure()
	assert not h.inserts


@pytest.mark.parametrize("doctype", ["Item", "Product Bundle"])
def test_create_permissions_checked_before_any_write(h, doctype):
	h.denied_create.add(doctype)
	with pytest.raises(PermissionError):
		h.ensure()
	assert not h.inserts


def test_source_and_existing_parent_read_permissions_are_enforced(h):
	parent, _ = h.manual()
	parent["denied"] = ["read"]
	with pytest.raises(PermissionError):
		h.ensure(planned_code="MANUAL")
	assert not h.inserts
	parent["denied"] = []
	h.data["Item"]["STICKER"]["denied"] = ["read"]
	with pytest.raises(PermissionError):
		h.ensure()
	assert not h.inserts


def test_generated_code_collision_and_removed_reviewed_master_do_not_create_alternate(h):
	code = h.module.bundle_code("CUST", "CHALK", "STICKER")
	h.data["Item"][code] = Doc(name=code)
	with pytest.raises(h.module.BundleInputError, match="不完整"):
		h.ensure()
	del h.data["Item"][code]
	with pytest.raises(h.module.BundleInputError, match="删除"):
		h.ensure(planned_code="MANUAL-DELETED")
	assert not h.inserts


def test_description_escapes_names_and_cache_cleanup_is_targeted(h):
	h.data["Item"]["CHALK"]["item_name"] = '<img src=x onerror="bad">'
	result = h.ensure()
	assert "<img" not in h.data["Item"][result["item_code"]].description
	h.module.clear_temporary_caches([result["item_code"], result["item_code"]])
	assert h.frappe.clear_document_cache.call_count == 2
	assert {call.args for call in h.frappe.clear_document_cache.call_args_list} == {
		("Item", result["item_code"]), ("Product Bundle", result["item_code"])}


def test_concurrent_duplicate_is_not_swallowed_or_committed(h):
	native_new_doc = h.frappe.new_doc

	def new_doc(doctype):
		doc = native_new_doc(doctype)
		if doctype == "Item":
			doc.insert = Mock(side_effect=h.frappe.DuplicateEntryError("racing creator"))
		return doc

	h.frappe.new_doc = new_doc
	with pytest.raises(h.frappe.DuplicateEntryError, match="racing creator"):
		h.ensure()
	assert not h.inserts
	h.frappe.db.commit.assert_not_called()
	h.frappe.db.rollback.assert_not_called()
