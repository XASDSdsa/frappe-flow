"""Identity, exact catalog matching, quantities and sticker ownership boundaries."""

import importlib.util
from pathlib import Path
import re
import sys
import types

import pytest


class Row(dict):
	__getattr__ = dict.get


class Doc(Row):
	def check_permission(self, permission):
		self.setdefault("permission_checks", []).append(permission)
		if self.get("denied"):
			raise PermissionError("restricted")


class Harness:
	def __init__(self, monkeypatch):
		self.queries, self.reads = [], []
		self.data = {
			"User": {"sales@example.com": Doc(name="sales@example.com", enabled=1, user_type="System User")},
			"Employee": {"EMP-1": Doc(name="EMP-1", user_id="sales@example.com", status="Active", denied=True)},
			"Sales Person": {"Alice": Doc(name="Alice", employee="EMP-1", enabled=1, is_group=0)},
			"Customer": {"CUST-1": Doc(name="CUST-1", customer_name="Exact Customer", disabled=0,
				customer_group="Wholesale", default_currency="EUR", default_price_list="Wholesale EUR")},
			"Item": {}, "UOM": {"Nos": Doc(name="Nos", must_be_whole_number=1), "Box": Doc(name="Box", must_be_whole_number=1)},
			"Product Bundle": {},
		}
		frappe = types.ModuleType("frappe")
		frappe.session = Row(user="sales@example.com")
		frappe.PermissionError = PermissionError
		frappe.DoesNotExistError = KeyError
		frappe.get_doc = self.get_doc
		frappe.get_all = self.get_all
		frappe.db = Row(get_value=self.get_value, exists=self.exists)
		utils = types.ModuleType("frappe.utils")
		utils.nowdate = lambda: "2026-09-17"
		conversion = types.ModuleType("erpnext.stock.doctype.item.item")
		conversion.get_uom_conv_factor = lambda *_: None
		monkeypatch.setitem(sys.modules, "frappe", frappe)
		monkeypatch.setitem(sys.modules, "frappe.utils", utils)
		monkeypatch.setitem(sys.modules, "erpnext.stock.doctype.item.item", conversion)
		spec = importlib.util.spec_from_file_location("sales_order_resolution_under_test",
			Path(__file__).resolve().parents[2] / "flow/integrations/erpnext/sales_order_resolution.py")
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)
		self.frappe = frappe
		self.item()

	def get_doc(self, doctype, name):
		self.reads.append((doctype, name))
		return self.data[doctype][name]

	def get_all(self, doctype, filters, fields=None, limit_page_length=0, **kwargs):
		self.queries.append((doctype, filters, fields))
		# Deliberately imitate case-insensitive MariaDB to test exact post-filtering.
		def equal(actual, value):
			if isinstance(value, list) and value[0] == "in":
				return actual in value[1]
			if isinstance(value, list) and value[0] == "like":
				return isinstance(actual, str) and re.fullmatch(re.escape(value[1]).replace("%", ".*").replace("_", "."), actual) is not None
			return actual.casefold() == value.casefold() if isinstance(actual, str) and isinstance(value, str) else actual == value
		rows = [row for row in self.data[doctype].values() if all(equal(row.get(key), value) for key, value in filters.items())]
		if limit_page_length:
			rows = rows[:limit_page_length]
		return [Row({key: row.get(key) for key in fields}) for row in rows]

	def get_value(self, doctype, name, fields, as_dict=False):
		row = self.data[doctype].get(name)
		return Row({key: row.get(key) for key in fields}) if row else None

	def exists(self, doctype, filters):
		return bool(self.get_all(doctype, filters, fields=["name"], limit_page_length=1))

	def item(self, code="GREEN", **kwargs):
		doc = Doc(name=code, item_name="Driven25 绿色", disabled=0, has_variants=0, is_sales_item=1, is_stock_item=1,
			stock_uom="Nos", sales_uom=None, attributes=[Row(attribute="贴纸型号", attribute_value="Driven25")], uoms=[])
		doc.update(kwargs)
		self.data["Item"][code] = doc
		return doc

	def sticker(self, code="STICKER", owner="Exact Customer", model="Driven25", version="v1", **kwargs):
		return self.item(code, item_name=f"Customer sticker {code}", variant_of="巧克粉贴纸",
			attributes=[Row(attribute="客户", attribute_value=owner), Row(attribute="贴纸型号", attribute_value=model),
				Row(attribute="贴纸版本", attribute_value=version)], **kwargs)

	def call(self, **kwargs):
		values = dict(customer="CUST-1", items=[{"item_code": "GREEN", "qty": 120}])
		values.update(kwargs)
		return self.module.resolve_order_inputs(**values)


@pytest.fixture
def h(monkeypatch):
	return Harness(monkeypatch)


def codes(result):
	return {issue["code"] for issue in result["issues"]}


def test_ready_identity_uses_only_current_users_fixed_internal_mapping(h):
	result = h.call()
	assert result["status"] == "ready"
	assert result["sales_team"] == [{"sales_person": "Alice", "allocated_percentage": 100}]
	assert result["session_user"] == "sales@example.com"
	assert result["delivery_date"] == "2026-09-24"
	assert result["delivery_deadline"] == "2026-09-24 23:59:00"
	assert result["customer"]["default_currency"] == "EUR"
	assert result["currency"] is None
	assert not any(dt in {"User", "Employee", "Sales Person"} for dt, _ in h.reads)
	assert next(query for query in h.queries if query[0] == "Employee")[1:] == ({"user_id": "sales@example.com", "status": "Active"}, ["name"])
	assert h.data["Customer"]["CUST-1"]["permission_checks"] == ["read"]
	assert h.data["Item"]["GREEN"]["permission_checks"] == ["read"]
	assert "commission_rate" not in repr(result)
	assert "EMP-1" not in repr(result)


@pytest.mark.parametrize("doctype,key,change,expected", [
	("User", "sales@example.com", {"enabled": 0}, "invalid_session_user"),
	("User", "sales@example.com", {"user_type": "Website User"}, "invalid_session_user"),
	("Employee", "EMP-1", {"status": "Left"}, "employee_mapping"),
	("Sales Person", "Alice", {"enabled": 0}, "sales_person_mapping"),
	("Sales Person", "Alice", {"is_group": 1}, "sales_person_mapping"),
])
def test_identity_rejects_inactive_and_website_users(h, doctype, key, change, expected):
	h.data[doctype][key].update(change)
	result = h.call()
	assert expected in codes(result)
	assert result["items"] == []


@pytest.mark.parametrize("doctype,key,expected", [("Employee", "EMP-1", "employee_mapping"), ("Sales Person", "Alice", "sales_person_mapping")])
def test_identity_duplicate_links_are_not_arbitrarily_selected(h, doctype, key, expected):
	h.data[doctype]["duplicate"] = Doc(**{**h.data[doctype][key], "name": "duplicate"})
	assert expected in codes(h.call())


def test_all_independent_missing_inputs_are_returned_in_one_pass(h):
	h.data["Employee"].clear()
	result = h.call(customer="Missing", delivery_date="tomorrow", items=[{"item_code": "Missing", "qty": -1}, {"item_name": "Driven25", "qty": "NaN"}])
	assert {"employee_mapping", "customer_not_found", "invalid_delivery_date", "item_not_found", "invalid_number"} <= codes(result)
	assert len([issue for issue in result["issues"] if issue["code"] == "invalid_number"]) == 2
	assert result["items"] == []


@pytest.mark.parametrize("qty", [True, False, 0, -1, "NaN", "Infinity", "-Infinity", "1e1000", "1e-1000", [], None])
def test_quantities_are_positive_finite_and_representable(h, qty):
	assert "invalid_number" in codes(h.call(items=[{"item_code": "GREEN", "qty": qty}]))


def test_exact_item_customer_names_and_document_permissions(h):
	assert h.call(customer="Exact Customer", items=[{"item_name": "Driven25 绿色", "qty": 1}])["status"] == "ready"
	assert "customer_not_found" in codes(h.call(customer="exact customer"))
	assert "item_not_found" in codes(h.call(items=[{"item_code": "green", "qty": 1}]))
	assert "item_not_found" in codes(h.call(items=[{"item_name": "Driven25", "qty": 1}]))
	h.data["Item"]["GREEN"]["denied"] = True
	result = h.call()
	assert "permission_denied" in codes(result)
	assert result["product_rows"] == []


def test_duplicate_names_disabled_templates_and_non_sales_are_actionable(h):
	h.item("DUPLICATE")
	assert "ambiguous_item" in codes(h.call(items=[{"item_name": "Driven25 绿色", "qty": 1}]))
	h.data["Item"]["GREEN"].update(disabled=1, has_variants=1, is_sales_item=0)
	assert {"disabled_item", "item_template", "not_sales_item"} <= codes(h.call())
	h.data["Customer"]["CUST-1"]["disabled"] = 1
	assert "disabled_customer" in codes(h.call())


def test_quantity_uses_native_sales_uom_and_rejects_missing_conversion(h):
	h.data["Item"]["GREEN"].update(sales_uom="Box", uoms=[Row(uom="Box", conversion_factor=12)])
	result = h.call(items=[{"item_code": "GREEN", "qty": 10}])
	assert result["product_rows"][0]["uom"] == "Box"
	assert result["product_rows"][0]["stock_qty"] == 120
	h.data["Item"]["GREEN"]["uoms"] = []
	assert "missing_uom_conversion" in codes(h.call())


def test_whole_number_units_and_converted_stock_quantities_are_checked(h):
	assert "whole_uom_required" in codes(h.call(items=[{"item_code": "GREEN", "qty": "1.5"}]))
	h.data["UOM"]["Box"]["must_be_whole_number"] = 0
	h.data["Item"]["GREEN"]["uoms"] = [Row(uom="Box", conversion_factor=2)]
	assert h.call(items=[{"item_code": "GREEN", "qty": ".5", "uom": "Box"}])["status"] == "ready"
	assert "whole_stock_uom_required" in codes(h.call(items=[{"item_code": "GREEN", "qty": ".25", "uom": "Box"}]))


@pytest.mark.parametrize("values,expected", [
	({"base_date": "2026-12-28"}, "2027-01-04"),
	({"base_date": "2028-02-23"}, "2028-03-01"),
	({"delivery_date": "2026-10-01", "delivery_days": 1}, "2026-10-01"),
	({"delivery_days": 0}, "2026-09-17"),
])
def test_calendar_delivery_is_explicit_and_end_of_day(h, values, expected):
	result = h.call(**values)
	assert result["delivery_date"] == expected
	assert result["delivery_deadline"] == expected + " 23:59:00"


@pytest.mark.parametrize("values,code", [
	({"delivery_days": True}, "invalid_delivery_days"),
	({"delivery_days": 1.5}, "invalid_delivery_days"),
	({"delivery_days": -1}, "invalid_delivery_days"),
	({"delivery_date": "20260924"}, "invalid_delivery_date"),
	({"delivery_date": "2026-09-24T23:59:00"}, "invalid_delivery_date"),
	({"delivery_date": "2026-02-30"}, "invalid_delivery_date"),
	({"delivery_date": "2026-09-16"}, "past_delivery_date"),
])
def test_invalid_delivery_inputs_are_not_coerced(h, values, code):
	assert code in codes(h.call(**values))


def test_driven25_four_colors_have_separate_product_and_free_sticker_rows(h):
	h.sticker()
	items = []
	for color, quantity in [("green", 120), ("gray", 120), ("blue", 120), ("pink", 40)]:
		h.item(color, item_name=f"Driven25 {color}")
		items.append({"item_code": color, "qty": quantity})
	result = h.call(items=items, include_stickers=True)
	assert result["status"] == "ready"
	assert [row["qty"] for row in result["sticker_rows"]] == [120, 120, 120, 40]
	assert [row["product_row"] for row in result["sticker_rows"]] == [1, 2, 3, 4]
	assert all(row["rate"] == 0 and row["is_free_item"] == 1 for row in result["sticker_rows"])
	assert all("rate" not in row and "is_free_item" not in row for row in result["product_rows"])
	assert len(result["items"]) == 8


def test_sticker_versions_require_choice_and_explicit_selection_resolves(h):
	h.sticker("V1")
	h.sticker("V2", version="v2")
	result = h.call(include_stickers=True)
	issue = next(issue for issue in result["issues"] if issue["code"] == "sticker_choice_required")
	assert {row["sticker_version"] for row in issue["choices"]} == {"v1", "v2"}
	assert result["items"] == []
	result = h.call(include_stickers=True, sticker_mappings=[{"product_row": 1, "sticker_model": "Driven25", "sticker_version": "v2"}])
	assert result["status"] == "ready"
	assert [row["item_code"] for row in result["sticker_rows"]] == ["V2"]


def test_sticker_owner_is_actual_attribute_not_item_name(h):
	h.sticker("Exact Customer-Driven25-v1", owner="Someone Else")
	result = h.call(sticker_mappings=[{"product_row": 1, "item_code": "Exact Customer-Driven25-v1"}])
	assert "sticker_customer_mismatch" in codes(result)
	assert result["sticker_rows"] == []
	assert "sticker_not_found" in codes(h.call(include_stickers=True))


def test_duplicate_customer_names_block_sticker_ownership_even_for_exact_id(h):
	h.sticker()
	h.data["Customer"]["CUST-2"] = Doc(name="CUST-2", customer_name="Exact Customer", disabled=0)
	assert "ambiguous_customer" in codes(h.call(customer="Exact Customer"))
	assert "ambiguous_sticker_owner" in codes(h.call(include_stickers=True))
	assert h.call()["status"] == "ready"


def test_no_model_relationship_is_guessed_even_for_only_customer_sticker(h):
	h.sticker()
	h.data["Item"]["GREEN"]["attributes"] = []
	result = h.call(include_stickers=True)
	assert "sticker_not_found" in codes(result)
	result = h.call(sticker_mappings=[{"product_row": 1, "item_code": "STICKER"}])
	assert result["status"] == "ready"


def test_sticker_quantity_defaults_to_product_physical_count_and_stays_free(h):
	h.sticker()
	h.data["Item"]["GREEN"].update(sales_uom="Box", uoms=[Row(uom="Box", conversion_factor=12)])
	result = h.call(items=[{"item_code": "GREEN", "qty": 10}], sticker_mappings=[{"product_row": 1, "item_code": "STICKER"}])
	assert result["sticker_rows"][0]["qty"] == 120
	assert result["sticker_rows"][0]["rate"] == 0
	assert result["sticker_rows"][0]["is_free_item"] == 1
	assert result["sticker_rows"][0]["bundle_eligible"] is True
	assert "sticker_must_be_free" in codes(h.call(sticker_mappings=[{"product_row": 1, "item_code": "STICKER", "rate": .15}]))
	assert "invalid_number" in codes(h.call(sticker_mappings=[{"product_row": 1, "item_code": "STICKER", "rate": -1}]))


def test_explicit_mapping_cannot_use_invalid_product_row_or_non_sticker(h):
	h.sticker()
	assert "invalid_product_row" in codes(h.call(sticker_mappings=[{"product_row": True, "item_code": "STICKER"}]))
	assert "invalid_product_row" in codes(h.call(sticker_mappings=[{"product_row": [], "item_code": "STICKER"}]))
	assert "invalid_sticker" in codes(h.call(sticker_mappings=[{"product_row": 1, "item_code": "GREEN"}]))
	assert "invalid_product_row" in codes(h.call(items=[{"item_code": "STICKER", "qty": 1}],
		sticker_mappings=[{"product_row": 1, "item_code": "STICKER"}]))


def test_sticker_only_order_is_allowed_with_warning_and_zero_rate(h):
	h.sticker()
	result = h.call(items=[{"item_code": "STICKER", "qty": 1000}], include_stickers=True)
	assert result["status"] == "ready"
	assert result["product_rows"] == result["sticker_rows"] == []
	assert result["items"] == result["standalone_sticker_rows"]
	assert result["items"][0]["row_type"] == "standalone_sticker"
	assert result["items"][0]["is_free_item"] == 1
	assert result["items"][0]["rate"] == 0
	assert result["warnings"][0]["code"] == "standalone_customer_stickers"
	assert result["missing"] == []


@pytest.mark.parametrize("values", [
	{"items": [{"item_code": "STICKER", "qty": 1000, "rate": .1}]},
	{"stickers_free": False},
])
def test_paid_stickers_require_separate_customization_fee(h, values):
	h.sticker()
	result = h.call(**values)
	assert "sticker_must_be_free" in codes(result)
	assert result["items"] == []
	assert "服务费" in result["issues"][0]["message"]


def test_independent_and_companion_stickers_do_not_change_product_mapping_or_default_prices(h):
	h.sticker()
	result = h.call(items=[{"item_code": "STICKER", "qty": 1000, "standalone": True},
		{"item_code": "GREEN", "qty": 120}], include_stickers=True)
	assert result["status"] == "ready"
	assert [row["row_type"] for row in result["items"]] == ["standalone_sticker", "product", "sticker"]
	assert result["product_rows"][0]["product_row"] == 2
	assert result["sticker_rows"][0]["product_row"] == 2
	assert result["sticker_rows"][0]["qty"] == 120
	assert result["sticker_rows"][0]["rate"] == 0
	assert result["standalone_sticker_rows"][0]["rate"] == 0


@pytest.mark.parametrize("changes,expected", [
	({"owner": "Other Customer"}, "sticker_customer_mismatch"),
	({"is_stock_item": 0}, "sticker_not_stock_item"),
	({"disabled": 1}, "disabled_item"),
	({"denied": True}, "permission_denied"),
])
def test_standalone_sticker_keeps_owner_stock_and_native_access_guards(h, changes, expected):
	h.sticker(**changes)
	result = h.call(items=[{"item_code": "STICKER", "qty": 1000}])
	assert expected in codes(result)
	assert result["items"] == []


def test_standalone_legacy_owner_is_exact_and_customer_names_must_be_unique(h):
	code = "巧克粉贴纸-Exact Customer-Driven25-v1"
	h.item(code, attributes=[])
	assert h.call(items=[{"item_code": code, "qty": 1000}])["status"] == "ready"
	h.data["Customer"]["CUST-2"] = Doc(name="CUST-2", customer_name="Exact Customer")
	assert "ambiguous_sticker_owner" in codes(h.call(items=[{"item_code": code, "qty": 1000}]))
	h.item("巧克粉贴纸-Other Customer-Driven25-v1", attributes=[])
	assert "invalid_sticker" in codes(h.call(items=[{"item_code": "巧克粉贴纸-Other Customer-Driven25-v1", "qty": 1000}]))


def test_disabled_and_unreadable_stickers_do_not_become_auto_candidates(h):
	h.sticker("OFF", disabled=1)
	h.sticker("PRIVATE", denied=True)
	result = h.call(include_stickers=True)
	assert "sticker_not_found" in codes(result)
	assert "PRIVATE" not in repr(result)
	assert "permission_denied" in codes(h.call(sticker_mappings=[{"product_row": 1, "item_code": "PRIVATE"}]))


def test_no_implicit_pricing_or_product_sticker_changes_without_request(h):
	h.sticker()
	result = h.call()
	assert result["status"] == "ready"
	assert result["sticker_rows"] == []
	assert not any(dt == "Item" and filters.get("variant_of") for dt, filters, _ in h.queries)
	assert "invalid_number" in codes(h.call(items=[{"item_code": "GREEN", "qty": 1, "rate": 0}]))


def test_legacy_driven25_exact_customer_and_full_product_model_match(h):
	h.data["Customer"]["CUST-1"]["customer_name"] = "Driven25"
	items = []
	for code, label, quantity in [("GREEN", "绿", 120), ("GRAY", "灰", 120), ("BLUE", "蓝", 120), ("PINK", "粉", 40)]:
		name = f"山东中性{label}方"
		h.item(f"SD-{code}-SQUARE-NEUTRAL", item_name=name, attributes=[])
		h.item(f"巧克粉贴纸-Driven25-{name}模版-v1", attributes=[])
		items.append({"item_code": f"SD-{code}-SQUARE-NEUTRAL", "qty": quantity})
	result = h.call(items=items, include_stickers=True)
	assert result["status"] == "ready"
	assert [row["qty"] for row in result["sticker_rows"]] == [120, 120, 120, 40]
	assert all(row["sticker_identity_source"] == "legacy_exact_customer_prefix" for row in result["sticker_rows"])
	assert sum(row["qty"] for row in result["sticker_rows"]) == 400


def test_legacy_template_spelling_equivalence_and_versions_still_require_choice(h):
	h.data["Item"]["GREEN"].update(item_name="山东中性绿方", attributes=[])
	h.item("巧克粉贴纸-Exact Customer-山东中性绿方模板-v1", attributes=[])
	assert h.call(include_stickers=True)["status"] == "ready"
	h.item("巧克粉贴纸-Exact Customer-山东中性绿方模版-v2", attributes=[])
	result = h.call(include_stickers=True)
	assert "sticker_choice_required" in codes(result)
	assert len(next(issue for issue in result["issues"] if issue["code"] == "sticker_choice_required")["choices"]) == 2


def test_legacy_prefix_never_overrides_wrong_customer_variant_attributes(h):
	code = "巧克粉贴纸-Exact Customer-Driven25-v1"
	h.sticker(code, owner="Someone Else")
	assert "sticker_customer_mismatch" in codes(h.call(sticker_mappings=[{"product_row": 1, "item_code": code}]))
	h.item("巧克粉贴纸-Other Customer-Driven25-v1", attributes=[])
	assert "invalid_sticker" in codes(h.call(sticker_mappings=[{"product_row": 1, "item_code": "巧克粉贴纸-Other Customer-Driven25-v1"}]))


def test_legacy_customer_prefix_collision_cannot_claim_another_customer_sticker(h):
	h.item("巧克粉贴纸-Exact Customer-Sub-Driven25-v1", attributes=[])
	assert "invalid_sticker" in codes(h.call(sticker_mappings=[{"product_row": 1, "item_code": "巧克粉贴纸-Exact Customer-Sub-Driven25-v1"}]))


def test_duplicate_explicit_mapping_does_not_silently_double_sticker_count(h):
	h.sticker()
	mapping = {"product_row": 1, "item_code": "STICKER", "qty": 120}
	result = h.call(sticker_mappings=[mapping, mapping])
	assert "duplicate_sticker_mapping" in codes(result)
	assert result["items"] == []


def test_explicit_positive_product_price_is_preserved_without_changing_free_stickers(h):
	h.sticker()
	result = h.call(items=[{"item_code": "GREEN", "qty": 120, "rate": "12.35"}], include_stickers=True)
	assert result["status"] == "ready"
	assert result["product_rows"][0]["rate"] == 12.35
	assert result["items"][0]["rate"] == 12.35
	assert result["sticker_rows"][0]["rate"] == 0


@pytest.mark.parametrize("rate", [0, -1, True, "NaN", "Infinity", "1e1000", None])
def test_explicit_product_price_must_be_positive_and_finite(h, rate):
	result = h.call(items=[{"item_code": "GREEN", "qty": 120, "rate": rate}])
	assert "invalid_number" in codes(result)
	assert result["items"] == []


def test_explicitly_free_nonstock_service_can_be_zero_priced(h):
	h.item("CUSTOM-SERVICE", item_name="贴纸制作模版收费500", is_stock_item=0)
	result = h.call(items=[{"item_code": "CUSTOM-SERVICE", "qty": 1, "rate": 0, "is_free_item": True}])
	assert result["status"] == "ready"
	assert result["items"][0]["rate"] == 0
	assert result["items"][0]["is_free_item"] == 1


def test_zero_priced_stock_item_cannot_be_marked_as_free_service(h):
	result = h.call(items=[{"item_code": "GREEN", "qty": 1, "rate": 0, "is_free_item": True}])
	assert "free_stock_item_not_allowed" in codes(result)
	assert result["items"] == []


def test_explicit_customization_service_defaults_to_free_real_order_row(h):
	h.item("SERVICE", item_name="贴纸定制服务500", is_stock_item=0)
	result = h.call(customization_services=[{"item_name": "贴纸定制服务500"}])
	assert result["status"] == "ready"
	service = result["items"][-1]
	assert (service["row_type"], service["item_code"], service["qty"], service["rate"], service["is_free_item"]) == (
		"service", "SERVICE", 1, 0, 1)
	assert service not in result["product_rows"]
	assert h.data["Item"]["SERVICE"]["permission_checks"] == ["read"]


def test_explicit_customization_service_keeps_agreed_price_and_quantity(h):
	h.sticker()
	h.item("SERVICE", is_stock_item=0)
	result = h.call(include_stickers=True,
		customization_services=[{"item_code": "SERVICE", "qty": 2, "rate": "100.50", "uom": "Nos"}])
	assert result["status"] == "ready"
	service = next(row for row in result["items"] if row["row_type"] == "service")
	assert (service["qty"], service["rate"], service["is_free_item"]) == (2, 100.5, 0)
	assert [row["item_code"] for row in result["product_rows"]] == ["GREEN"]
	assert [row["product_item_code"] for row in result["sticker_rows"]] == ["GREEN"]


@pytest.mark.parametrize("services", [None, []])
def test_sticker_selection_never_infers_a_new_customization_service(h, services):
	h.sticker()
	h.item("SERVICE", is_stock_item=0)
	for include_stickers in (False, True):
		result = h.call(include_stickers=include_stickers, customization_services=services)
		assert result["status"] == "ready"
		assert all(row["row_type"] != "service" for row in result["items"])


@pytest.mark.parametrize("changes,expected", [
	({"is_stock_item": 1}, "invalid_customization_service"),
	({"variant_of": "巧克粉贴纸"}, "invalid_customization_service"),
	({"denied": True}, "permission_denied"),
	({"disabled": 1}, "disabled_item"),
	({"is_sales_item": 0}, "not_sales_item"),
])
def test_customization_services_retain_native_item_boundaries(h, changes, expected):
	h.item("SERVICE", **{"is_stock_item": 0, **changes})
	result = h.call(customization_services=[{"item_code": "SERVICE"}])
	assert expected in codes(result)
	assert result["items"] == []


@pytest.mark.parametrize("rate", [-1, "NaN", "Infinity", True, None, "1e400", "1e-400"])
def test_customization_service_rejects_invalid_prices(h, rate):
	h.item("SERVICE", is_stock_item=0)
	result = h.call(customization_services=[{"item_code": "SERVICE", "rate": rate}])
	assert "invalid_number" in codes(result)
	assert result["items"] == []


@pytest.mark.parametrize("services,expected", [
	("SERVICE", "invalid_customization_services"),
	([{}] * 101, "invalid_customization_services"),
	(["SERVICE"], "invalid_service_row"),
	([{"item_code": "SERVICE", "qty": 0}], "invalid_number"),
	([{"item_code": "SERVICE", "is_free_item": True}], "unsupported_fields"),
])
def test_customization_service_input_is_bounded_and_explicit(h, services, expected):
	h.item("SERVICE", is_stock_item=0)
	result = h.call(customization_services=services)
	assert expected in codes(result)
	assert result["items"] == []


def test_legacy_free_service_is_classified_and_duplicate_entry_is_rejected(h):
	h.item("SERVICE", item_name="贴纸定制服务500", is_stock_item=0)
	items = [{"item_code": "SERVICE", "qty": 1, "rate": 0, "is_free_item": True}]
	result = h.call(items=items)
	assert result["status"] == "ready"
	assert result["items"][0]["row_type"] == "service"
	assert result["product_rows"] == []
	result = h.call(items=items, customization_services=[{"item_name": "贴纸定制服务500"}])
	assert "duplicate_service_input" in codes(result)
	assert result["items"] == []


def test_customization_services_reject_repeated_item_without_silent_merging(h):
	h.item("SERVICE", item_name="贴纸定制服务500", is_stock_item=0)
	result = h.call(customization_services=[{"item_code": "SERVICE"}, {"item_name": "贴纸定制服务500"}])
	assert "duplicate_service_input" in codes(result)
	assert "一行并明确数量" in result["issues"][0]["message"]
	assert result["items"] == []


@pytest.mark.parametrize("disabled", [0, 1])
@pytest.mark.parametrize("legacy_input", [False, True])
def test_non_stock_bundle_parent_cannot_become_customization_service(h, disabled, legacy_input):
	h.item("BUNDLE", is_stock_item=0)
	h.data["Product Bundle"]["BUNDLE-DEFINITION"] = Doc(name="BUNDLE-DEFINITION", new_item_code="BUNDLE", disabled=disabled)
	values = ({"items": [{"item_code": "BUNDLE", "qty": 1, "is_free_item": True, "rate": 0}]}
		if legacy_input else {"customization_services": [{"item_code": "BUNDLE"}]})
	result = h.call(**values)
	assert "bundle_not_service" in codes(result)
	assert result["items"] == []
	assert h.data["Item"]["BUNDLE"]["permission_checks"] == ["read"]
	assert "BUNDLE-DEFINITION" not in repr(result)


@pytest.mark.parametrize("color", list("绿灰蓝粉"))
def test_documented_shorthand_resolves_only_exact_neutral_full_name(h, color):
	h.item("SHORT", item_name=f"山东中性{color}方", attributes=[])
	result = h.call(items=[{"item_name": f"山东{color}方", "qty": 120}])
	assert result["status"] == "ready"
	assert result["product_rows"][0]["item_code"] == "SHORT"
	assert result["product_rows"][0]["item_name"] == f"山东中性{color}方"


def test_shorthand_returns_only_readable_enabled_leaf_choices_when_ambiguous(h):
	for code, changes in [("ONE", {}), ("TWO", {}), ("DISABLED", {"disabled": 1}),
		("TEMPLATE", {"has_variants": 1}), ("NO_SALES", {"is_sales_item": 0}), ("PRIVATE", {"denied": True})]:
		h.item(code, item_name="山东中性绿方", **changes)
	result = h.call(items=[{"item_name": "山东绿方", "qty": 120}])
	issue = next(issue for issue in result["issues"] if issue["code"] == "ambiguous_item")
	assert {choice["item_code"] for choice in issue["choices"]} == {"ONE", "TWO"}
	assert result["items"] == []
	assert "PRIVATE" not in repr(result)


def test_shorthand_is_not_applied_to_other_colors_partial_names_or_item_codes(h):
	h.item("WHITE", item_name="山东中性白方")
	h.item("EXTRA", item_name="山东中性绿方加强版")
	for values in [{"item_name": "山东白方"}, {"item_name": "绿方"}, {"item_name": "山东绿方"}, {"item_code": "山东绿方"}]:
		assert "item_not_found" in codes(h.call(items=[{**values, "qty": 120}]))


def test_existing_matching_rows_are_consumed_once_and_keep_original_indexes(h):
	h.sticker()
	result = h.call(items=[{"item_code": "STICKER", "qty": 120}, {"item_code": "GREEN", "qty": 120}], include_stickers=True)
	assert result["status"] == "ready"
	assert [row["row_type"] for row in result["items"]] == ["product", "sticker"]
	assert result["standalone_sticker_rows"] == []
	assert result["sticker_rows"][0]["input_sticker_row"] == 1
	assert result["sticker_rows"][0]["product_row"] == 2
	assert result["sticker_rows"][0]["bundle_eligible"] is True
	assert result["warnings"] == []


def test_explicit_context_pairing_does_not_require_rigid_name_or_model_match(h):
	h.sticker(model="客户手绘标识")
	result = h.call(items=[{"item_code": "GREEN", "qty": 120}, {"item_code": "STICKER", "qty": 120}],
		bundle_mappings=[{"product_row": 1, "sticker_row": 2}])
	assert result["status"] == "ready"
	assert result["sticker_rows"][0]["sticker_model"] == "客户手绘标识"
	assert len(result["items"]) == 2


def test_two_versions_or_unknown_relationship_require_choices_not_input_order(h):
	h.sticker("V1")
	h.sticker("V2", version="v2")
	result = h.call(items=[{"item_code": "GREEN", "qty": 120},
		{"item_code": "V1", "qty": 120}, {"item_code": "V2", "qty": 120}])
	assert "bundle_mapping_required" in codes(result)
	assert result["items"] == []
	assert result["sticker_rows"] == []
	choices = next(issue["choices"] for issue in result["issues"] if issue["code"] == "bundle_mapping_required")
	assert {choice["sticker_row"] for choice in choices} == {2, 3}
	assert all(choice["product_candidates"][0]["product_row"] == 1 for choice in choices)
	result = h.call(items=[{"item_code": "GREEN", "qty": 120},
		{"item_code": "V1", "qty": 120}, {"item_code": "V2", "qty": 120, "standalone": True}])
	assert result["status"] == "ready"
	assert [row["item_code"] for row in result["sticker_rows"]] == ["V1"]
	assert [row["item_code"] for row in result["standalone_sticker_rows"]] == ["V2"]


def test_pairing_uses_stock_quantity_and_never_guesses_partial_consumption(h):
	h.sticker()
	h.data["Item"]["GREEN"].update(sales_uom="Box", uoms=[Row(uom="Box", conversion_factor=12)])
	items = [{"item_code": "GREEN", "qty": 10}, {"item_code": "STICKER", "qty": 120}]
	assert h.call(items=items)["status"] == "ready"
	items[1]["qty"] = 1000
	assert "bundle_mapping_required" in codes(h.call(items=items))
	result = h.call(items=items, bundle_mappings=[{"product_row": 1, "sticker_row": 2}])
	assert "bundle_quantity_mismatch" in codes(result)
	assert result["items"] == []
	result = h.call(sticker_mappings=[{"product_row": 1, "item_code": "STICKER", "qty": 1}])
	assert "bundle_quantity_mismatch" in codes(result)


def test_duplicate_product_rows_can_pair_separately_without_merging(h):
	h.sticker()
	items = [{"item_code": "GREEN", "qty": 40}, {"item_code": "GREEN", "qty": 120},
		{"item_code": "STICKER", "qty": 120}, {"item_code": "STICKER", "qty": 40}]
	result = h.call(items=items)
	assert result["status"] == "ready"
	assert [(row["product_row"], row["input_sticker_row"], row["stock_qty"]) for row in result["sticker_rows"]] == [(1, 4, 40), (2, 3, 120)]
	assert len(result["items"]) == 4
	items[0]["qty"] = items[3]["qty"] = 120
	assert "bundle_mapping_required" in codes(h.call(items=items))
	result = h.call(items=items, bundle_mappings=[{"product_row": 1, "sticker_row": 4}, {"product_row": 2, "sticker_row": 3}])
	assert result["status"] == "ready"
	assert len(result["product_rows"]) == len(result["sticker_rows"]) == 2


@pytest.mark.parametrize("mappings,expected", [
	([{"product_row": 1, "sticker_row": 2}, {"product_row": 1, "sticker_row": 2}], "duplicate_bundle_mapping"),
	([{"product_row": True, "sticker_row": 2}], "invalid_product_row"),
	([{"product_row": 1, "sticker_row": []}], "invalid_sticker_row"),
	([{"product_row": 1, "sticker_row": 1}], "invalid_sticker_row"),
	([{"product_row": 1, "sticker_row": 2, "qty": 50}], "unsupported_fields"),
])
def test_bundle_mapping_rejects_duplicate_or_invalid_source_rows(h, mappings, expected):
	h.sticker()
	result = h.call(items=[{"item_code": "GREEN", "qty": 120}, {"item_code": "STICKER", "qty": 120}], bundle_mappings=mappings)
	assert expected in codes(result)
	assert result["items"] == []


def test_explicit_standalone_cannot_be_consumed_and_unmentioned_items_are_not_bundled(h):
	h.sticker()
	items = [{"item_code": "GREEN", "qty": 120}, {"item_code": "STICKER", "qty": 120, "standalone": True}]
	result = h.call(items=items)
	assert result["status"] == "ready"
	assert result["sticker_rows"] == []
	assert len(result["standalone_sticker_rows"]) == 1
	assert "standalone_bundle_conflict" in codes(h.call(items=items, bundle_mappings=[{"product_row": 1, "sticker_row": 2}]))
	assert h.call()["sticker_rows"] == []


def test_multiple_legacy_stickers_cannot_double_the_product_bundle(h):
	h.sticker("V1")
	h.sticker("V2", version="v2")
	result = h.call(sticker_mappings=[{"product_row": 1, "item_code": "V1"}, {"product_row": 1, "item_code": "V2"}])
	assert "multiple_stickers_for_product" in codes(result)
	assert result["items"] == []
