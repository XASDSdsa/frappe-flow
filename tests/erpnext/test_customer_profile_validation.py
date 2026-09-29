"""Focused profile normalization tests with real phone parsing, without a database."""

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest


MODULE = Path(__file__).resolve().parents[2] / "flow/integrations/erpnext/customer_profile_validation.py"


class ValidationError(ValueError):
	pass


class Doc(dict):
	@property
	def name(self):
		return self["name"]

	def check_permission(self, permission):
		if self.get("no_read"):
			raise PermissionError("无读取权限")


@pytest.fixture
def env(monkeypatch):
	data = {
		"User": {"agent@example.com": {"enabled": 1, "user_type": "System User"},
			"other@example.com": {"enabled": 1, "user_type": "System User"},
			"disabled@example.com": {"enabled": 0, "user_type": "System User"},
			"portal@example.com": {"enabled": 1, "user_type": "Website User"}},
		"Country": {"United States": {"code": "us"}, "China": {"code": "cn"}, "Hong Kong": {"code": "hk"}, "Malaysia": {"code": "my"}},
		"Customer Group": {"Commercial": {"is_group": 0}, "Individual": {"is_group": 0},
			"Government": {"is_group": 0}, "All Customers": {"is_group": 1}},
		"Territory": {"USA": {"territory_name": "United States"}, "China": {"territory_name": "China"}},
		"Currency": {"USD": {"enabled": 1}, "CNY": {"enabled": 1}, "EUR": {"enabled": 0}},
		"Price List": {"US Retail": {"selling": 1, "enabled": 1, "currency": "USD"},
			"CN Retail": {"selling": 1, "enabled": 1, "currency": "CNY"}},
		"Payment Terms Template": {"Net 30": {}}, "Tax Category": {"Export": {}},
		"Language": {"en": {"enabled": 1}},
	}
	for doctype, rows in data.items():
		for name, values in rows.items():
			values.update(name=name, doctype=doctype)
	defaults, user_defaults = {}, {}
	frappe = types.ModuleType("frappe")
	frappe.session = types.SimpleNamespace(user="agent@example.com")
	frappe.ValidationError = ValidationError
	frappe.PermissionError = PermissionError
	frappe.throw = lambda message: (_ for _ in ()).throw(ValidationError(message))
	frappe.db = types.SimpleNamespace(exists=lambda doctype, name: name in data.get(doctype, {}))
	frappe.get_doc = lambda doctype, name: Doc(data[doctype][name])
	frappe.get_list = lambda doctype, filters, **kwargs: [Doc(row) for row in data.get(doctype, {}).values()
		if all(row.get(key) == value for key, value in filters.items())][:kwargs.get("limit_page_length", 100)]
	frappe.defaults = types.SimpleNamespace(get_user_default=user_defaults.get, get_global_default=defaults.get)
	utils = types.ModuleType("frappe.utils")
	def email(value, throw=False):
		if not re.fullmatch(r"[^\s@,]+@[^\s@,]+\.[^\s@,]+", value):
			raise ValidationError("Invalid email")
		return value
	utils.validate_email_address = email
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)
	spec = importlib.util.spec_from_file_location("customer_profile_validation_test", MODULE)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	values = dict(customer_name="Acme", customer_type="Company", customer_group="Commercial",
		territory="USA", default_currency="USD", contact_name="Mary Smith", phone_no="+1 202 456 1111",
		address_line1="1600 Pennsylvania Avenue NW", city="Washington", state="DC", country="US", pincode="20500",
		email_id="mary@example.com", address_type="Shipping", billing_address_mode="same_as_shipping")
	return types.SimpleNamespace(module=module, data=data, defaults=defaults, user_defaults=user_defaults, values=values)


def test_complete_profile_maps_native_children_and_normalizes_country(env):
	result = env.module.normalize_profile(env.values)
	assert result["missing"] == []
	assert result["shipping"]["country"] == "United States"
	assert result["shipping"]["phone"] == "+1 2024561111"
	assert result["contact"]["company_name"] == "Acme"
	assert result["contact"]["email_ids"] == [{"email_id": "mary@example.com", "is_primary": 1}]
	assert result["contact"]["phone_nos"][0]["is_primary_phone"] == 1
	assert result["contact"]["phone_nos"][0]["is_primary_mobile_no"] == 0
	assert result["billing"] is None


def test_missing_fields_return_together_without_inventing_defaults(env):
	result = env.module.normalize_profile({"customer_name": "Acme"})
	assert set(result["missing"]) == {"customer_type", "customer_group", "default_currency",
		"address_line1", "city", "country", "pincode", "contact_name", "mobile_no_or_phone_no", "address_type", "billing_address_mode"}
	assert result["customer"] == {"customer_name": "Acme", "account_manager": "agent@example.com"}
	assert result["warnings"]


def test_company_contact_required_and_individual_can_use_own_name(env):
	env.values.pop("contact_name")
	assert "contact_name" in env.module.normalize_profile(env.values)["missing"]
	env.values["customer_type"] = "Individual"
	result = env.module.normalize_profile(env.values)
	assert result["contact"]["first_name"] == "Acme"
	assert "company_name" not in result["contact"]


def test_city_planner_receives_unset_territory_instead_of_country_default(env):
	for key in ("customer_group", "territory", "default_currency"):
		env.values.pop(key)
	env.defaults.update(customer_group="Commercial", default_currency="USD")
	result = env.module.normalize_profile(env.values)
	assert not result["missing"]
	assert "territory" not in result["customer"]
	assert "territory" not in result["defaults_used"]
	env.data["Territory"]["USA2"] = dict(name="USA2", territory_name="United States")
	env.defaults["territory"] = "China"
	assert "territory" not in env.module.normalize_profile(env.values)["customer"]


def test_invalid_default_is_not_used(env):
	env.values.pop("default_currency")
	env.defaults["default_currency"] = "EUR"
	result = env.module.normalize_profile(env.values)
	assert "default_currency" in result["missing"]
	assert "default_currency" not in result["customer"]


@pytest.mark.parametrize("updates", [
	{"customer_group": "All Customers"}, {"default_currency": "EUR"},
	{"default_price_list": "CN Retail"}, {"customer_type": "Unknown"},
	{"website": "javascript:alert(1)"}, {"phone_no": "123"}, {"email_id": "broken"},
	{"customer_name": "a\nb"}, {"customer_name": "a" * 141},
	{"postal_code_not_applicable": "false"}, {"unexpected": "value"},
	{"billing_address": {"unexpected": "value"}}, {"billing_address": False},
])
def test_invalid_values_are_rejected(env, updates):
	with pytest.raises(ValidationError):
		env.module.normalize_profile({**env.values, **updates})


def test_supplied_link_must_be_readable(env):
	env.data["Customer Group"]["Commercial"]["no_read"] = True
	with pytest.raises(PermissionError):
		env.module.normalize_profile(env.values)


def test_separate_billing_address_requires_its_own_complete_details(env):
	env.values.pop("billing_address_mode")
	env.values["billing_address"] = {"address_line1": "1 Queens Road", "city": "Hong Kong", "country": "香港",
		"postal_code_not_applicable": True}
	result = env.module.normalize_profile(env.values)
	assert not result["missing"]
	assert result["billing"]["address_type"] == "Billing"
	assert result["billing"]["country"] == "Hong Kong"
	assert result["warnings"]
	env.values["billing_address"] = {"country": "US"}
	assert set(env.module.normalize_profile(env.values)["missing"]) == {
		"billing_address.address_line1", "billing_address.city", "billing_address.state", "billing_address.pincode"}


def test_existing_fields_are_retained_without_empty_replacement(env):
	env.values["default_price_list"] = ""
	result = env.module.normalize_profile(env.values, existing={"default_price_list": "US Retail"})
	assert result["customer"]["default_price_list"] == "US Retail"
	assert "customer_details" not in result["customer"]


def test_mobile_field_is_not_reclassified_by_number_database(env):
	env.values.pop("phone_no")
	env.values.update(mobile_no="010-59928888", country="中国", city="北京", state="北京", pincode="100000")
	result = env.module.normalize_profile(env.values)
	assert result["contact"]["phone_nos"] == [{"phone": "+86 1059928888", "is_primary_phone": 0, "is_primary_mobile_no": 1}]
	assert result["shipping"]["phone"] == "+86 1059928888"
	assert not any("固定电话" in warning for warning in result["warnings"])


def test_mobile_and_landline_can_both_be_saved(env):
	env.values.update(mobile_no="13800138000", phone_no="01059928888", country="CN", city="北京", state="北京")
	rows = env.module.normalize_profile(env.values)["contact"]["phone_nos"]
	assert len(rows) == 2
	assert sum(row["is_primary_phone"] for row in rows) == 1
	assert sum(row["is_primary_mobile_no"] for row in rows) == 1
	assert rows == [
		{"phone": "+86 1059928888", "is_primary_phone": 1, "is_primary_mobile_no": 0},
		{"phone": "+86 13800138000", "is_primary_phone": 0, "is_primary_mobile_no": 1},
	]


@pytest.mark.parametrize(("customer_type", "expected"), [
	("Company", "Commercial"), ("Partnership", "Commercial"), ("Individual", "Individual"),
])
def test_customer_type_drives_default_group_even_when_configured_defaults_differ(env, customer_type, expected):
	env.values.pop("customer_group")
	env.values["customer_type"] = customer_type
	env.defaults["customer_group"] = "Government"
	env.user_defaults["Customer Group"] = "Government"
	result = env.module.normalize_profile(env.values)
	assert result["customer"]["customer_group"] == expected
	assert result["defaults_used"]["customer_group"] == {"value": expected, "source": "customer_type"}
	assert "customer_group" not in result["missing"]


def test_explicit_customer_group_is_honored(env):
	result = env.module.normalize_profile(env.values | {"customer_group": "Government"})
	assert result["customer"]["customer_group"] == "Government"
	assert "customer_group" not in result["defaults_used"]


def test_existing_customer_group_is_preserved(env):
	env.values.pop("customer_group")
	result = env.module.normalize_profile(env.values, existing={"customer_group": "Government", "account_manager": "agent@example.com"})
	assert result["customer"]["customer_group"] == "Government"
	assert result["defaults_used"]["customer_group"]["source"] == "existing_customer"


def test_missing_default_group_is_reported_instead_of_silently_using_other_group(env):
	env.values.pop("customer_group")
	del env.data["Customer Group"]["Commercial"]
	env.defaults["customer_group"] = "Government"
	with pytest.raises(ValidationError, match="Commercial"):
		env.module.normalize_profile(env.values)


@pytest.mark.parametrize(("value", "expected"), [
	("+12107607172", "+1 2107607172"),
	("+1 2107607172", "+1 2107607172"),
	("+86 138 0013 8000", "+86 13800138000"),
	("+39 02 36618 300", "+39 0236618300"),
])
def test_phone_format_has_single_country_separator_and_preserves_significant_zero(env, value, expected):
	env.values.pop("phone_no")
	env.values["mobile_no"] = value
	result = env.module.normalize_profile(env.values)
	assert result["contact"]["phone_nos"] == [{"phone": expected, "is_primary_phone": 0, "is_primary_mobile_no": 1}]
	assert result["shipping"]["phone"] == expected


def test_same_phone_with_different_input_format_creates_one_contact_row(env):
	env.values.update(phone_no="+1 202 456 1111", mobile_no="+12024561111")
	result = env.module.normalize_profile(env.values)
	assert result["contact"]["phone_nos"] == [{"phone": "+1 2024561111", "is_primary_phone": 1, "is_primary_mobile_no": 1}]


def test_billing_phone_uses_same_country_separator_format(env):
	env.values["billing_address_mode"] = "separate"
	env.values["billing_address"] = {"address_line1": "1600 Pennsylvania Avenue NW", "city": "Washington",
		"state": "DC", "country": "US", "pincode": "20500", "phone": "+1 (202) 456-1111"}
	result = env.module.normalize_profile(env.values)
	assert result["billing"]["phone"] == "+1 2024561111"


def test_missing_country_does_not_fail_local_phone_parsing(env):
	env.values.update(country="", phone_no="020 7946 0000")
	assert "country" in env.module.normalize_profile(env.values)["missing"]


def test_company_base_currency_is_not_customer_transaction_default(env):
    env.values.pop("default_currency")
    env.defaults.update(Currency="CNY", currency="CNY")
    result = env.module.normalize_profile(env.values)
    assert "default_currency" in result["missing"]
    result = env.module.normalize_profile(env.values | {"default_price_list": "US Retail"})
    assert result["customer"]["default_currency"] == "USD"
    assert result["defaults_used"]["default_currency"]["source"] == "selected_price_list"


def test_unmodified_optional_existing_links_need_no_unrelated_permission(env):
    env.data["Payment Terms Template"]["Net 30"]["no_read"] = True
    result = env.module.normalize_profile(env.values, existing={"payment_terms": "Net 30", "account_manager": "agent@example.com"})
    assert not result["missing"]
    assert "payment_terms" not in result["customer"]


def test_customer_notes_preserve_line_breaks(env):
    result = env.module.normalize_profile(env.values | {"customer_details": "Retail customer\nPrefers email"})
    assert result["customer"]["customer_details"] == "Retail customer\nPrefers email"


def test_new_customer_defaults_responsibility_to_current_user(env):
    result = env.module.normalize_profile(env.values)
    assert result["customer"]["account_manager"] == "agent@example.com"
    assert result["defaults_used"]["account_manager"]["source"] == "current_user"


def test_explicit_responsible_user_is_preserved(env):
    result = env.module.normalize_profile(env.values | {"account_manager": "other@example.com"})
    assert result["customer"]["account_manager"] == "other@example.com"
    assert "account_manager" not in result["defaults_used"]


@pytest.mark.parametrize("manager", ["Guest", "unknown@example.com", "disabled@example.com", "portal@example.com"])
def test_invalid_responsible_users_are_rejected(env, manager):
    with pytest.raises(ValidationError):
        env.module.normalize_profile(env.values | {"account_manager": manager})


def test_existing_responsibility_is_not_changed_to_current_operator(env):
    env.data["User"]["other@example.com"]["no_read"] = True
    result = env.module.normalize_profile(env.values, existing={"account_manager": "other@example.com"})
    assert result["customer"]["account_manager"] == "other@example.com"
    assert result["defaults_used"]["account_manager"]["source"] == "existing_customer"


def test_unassigned_existing_customer_requires_explicit_responsibility(env):
    result = env.module.normalize_profile(env.values, existing={"customer_name": "Acme"})
    assert "account_manager" in result["missing"]
    assert "account_manager" not in result["customer"]
    result = env.module.normalize_profile(env.values | {"account_manager": "other@example.com"}, existing={"customer_name": "Acme"})
    assert "account_manager" not in result["missing"]
    assert result["customer"]["account_manager"] == "other@example.com"


def test_explicit_responsible_user_must_be_selectable(env):
    env.data["User"]["other@example.com"]["no_read"] = True
    with pytest.raises(PermissionError):
        env.module.normalize_profile(env.values | {"account_manager": "other@example.com"})


def forwarder_values(env):
    values = env.values | dict(address_type="货代收货", billing_address_mode="not_provided",
        country="CN", city="Guangzhou", state="Guangdong", pincode="510545",
        address_line1="53 Zhonghe Road", customer_country="马来西亚", shipping_phone="13800138000",
        shipping_contact_name="运道仓库", contact_phone_not_provided=True)
    values.pop("phone_no")
    values.pop("territory")
    return values


def test_explicit_direct_shared_address_policy_is_preserved(env):
    result = env.module.normalize_profile(env.values)
    assert result["address_policy"] == {"shipping_type": "Shipping", "billing_mode": "same_as_shipping"}
    assert result["territory_location"] == {"country": "United States", "city": "Washington", "state": "DC", "allow_country_only": False}
    assert result["shipping"]["address_type"] == "Shipping"


def test_old_ambiguous_input_requires_purpose_and_billing_decision(env):
    env.values.pop("address_type")
    env.values.pop("billing_address_mode")
    result = env.module.normalize_profile(env.values)
    assert set(result["missing"]) == {"address_type", "billing_address_mode"}
    assert result["address_policy"] == {"shipping_type": "", "billing_mode": ""}
    assert result["shipping"]["address_type"] == ""


def test_direct_address_may_have_no_billing_without_inventing_one(env):
    result = env.module.normalize_profile(env.values | {"billing_address_mode": "未提供"})
    assert result["missing"] == []
    assert result["billing"] is None
    assert result["address_policy"]["billing_mode"] == "not_provided"
    assert any("不把收货地址" in message for message in result["warnings"])


def test_forwarder_unknown_billing_preserves_actual_country_and_separate_phone(env):
    result = env.module.normalize_profile(forwarder_values(env))
    assert result["missing"] == []
    assert result["shipping"]["address_type"] == "货代收货"
    assert result["shipping"]["country"] == "China"
    assert result["shipping"]["phone"] == "+86 13800138000"
    assert result["shipping"]["address_title"] == "运道仓库"
    assert "email_id" not in result["shipping"]
    assert "phone_nos" not in result["contact"]
    assert result["contact"]["email_ids"][0]["email_id"] == "mary@example.com"
    assert result["territory_location"] == {"country": "Malaysia", "city": "", "state": "", "allow_country_only": True}
    assert result["billing"] is None
    assert any("客户本人电话未提供" in message for message in result["warnings"])


def test_forwarder_customer_local_phone_uses_customer_country_not_shipping_country(env):
    values = forwarder_values(env) | {"mobile_no": "0123456789", "contact_phone_not_provided": False}
    result = env.module.normalize_profile(values)
    assert result["missing"] == []
    assert result["contact"]["phone_nos"] == [{"phone": "+60 123456789", "is_primary_phone": 0, "is_primary_mobile_no": 1}]
    assert result["shipping"]["phone"] == "+86 13800138000"


def test_forwarder_requires_customer_country_even_when_shipping_country_known(env):
    values = forwarder_values(env)
    values.pop("customer_country")
    result = env.module.normalize_profile(values)
    assert "customer_country" in result["missing"]
    assert result["territory_location"]["country"] == ""
    assert result["territory_location"]["city"] == ""


def unknown_country_forwarder_values(env):
    values = forwarder_values(env) | {"customer_country_not_provided": True}
    values.pop("customer_country")
    return values


def test_explicit_unknown_customer_country_keeps_customer_geography_blank(env):
    result = env.module.normalize_profile(unknown_country_forwarder_values(env))
    assert result["missing"] == []
    assert result["territory_location"] == {"country": "", "city": "", "state": "",
        "allow_country_only": True, "country_not_provided": True}
    assert "territory" not in result["customer"]
    assert "customer_country_not_provided" not in result["customer"]
    assert result["shipping"]["country"] == "China"
    assert result["shipping"]["phone"] == "+86 13800138000"
    assert "phone_nos" not in result["contact"]
    assert any("国家未知、待补充" in warning for warning in result["warnings"])


@pytest.mark.parametrize("flag", [None, False])
def test_unknown_country_requires_explicit_true_flag(env, flag):
    values = unknown_country_forwarder_values(env)
    if flag is None:
        values.pop("customer_country_not_provided")
    else:
        values["customer_country_not_provided"] = flag
    result = env.module.normalize_profile(values)
    assert "customer_country" in result["missing"]
    assert not result["territory_location"].get("country_not_provided")


@pytest.mark.parametrize("flag", [None, "true", "false", 0, 1, [], {}])
def test_customer_unknown_country_flag_requires_boolean(env, flag):
    with pytest.raises(ValidationError, match="customer_country_not_provided 必须为 true 或 false"):
        env.module.normalize_profile(unknown_country_forwarder_values(env) | {"customer_country_not_provided": flag})


@pytest.mark.parametrize("geography", [
    {"customer_country": "Malaysia"}, {"customer_state": "Selangor"}, {"customer_city": "Kuala Lumpur"},
])
def test_unknown_customer_country_conflicts_with_supplied_customer_geography(env, geography):
    with pytest.raises(ValidationError, match="已提供客户国家、省州或城市"):
        env.module.normalize_profile(unknown_country_forwarder_values(env) | geography)


@pytest.mark.parametrize("address_type", ["Shipping", ""])
def test_unknown_customer_country_requires_explicit_forwarder(env, address_type):
    with pytest.raises(ValidationError, match="只有明确货代收货"):
        env.module.normalize_profile(env.values | {"address_type": address_type, "customer_country_not_provided": True})


@pytest.mark.parametrize(("phone_key", "phone", "expected"), [
    ("mobile_no", "+60 123456789", "+60 123456789"),
    ("phone_no", "+1 202 456 1111", "+1 2024561111"),
])
def test_unknown_country_accepts_international_phone_without_inferring_country(env, phone_key, phone, expected):
    values = unknown_country_forwarder_values(env) | {phone_key: phone, "contact_phone_not_provided": False}
    result = env.module.normalize_profile(values)
    assert result["missing"] == []
    assert result["territory_location"]["country"] == ""
    assert result["territory_location"]["country_not_provided"] is True
    assert result["contact"]["phone_nos"][0]["phone"] == expected
    assert result["shipping"]["phone"] == "+86 13800138000"


@pytest.mark.parametrize("phone_key", ["mobile_no", "phone_no"])
def test_unknown_country_local_customer_phone_requires_international_prefix(env, phone_key):
    # A valid Chinese number must not pick up the forwarding warehouse's country.
    values = unknown_country_forwarder_values(env) | {phone_key: "13800138000", "contact_phone_not_provided": False}
    with pytest.raises(ValidationError, match="必须填写带国家区号（以 \\+ 开头）"):
        env.module.normalize_profile(values)


@pytest.mark.parametrize("shipping_field", ["address_line1", "country", "city", "state", "pincode", "shipping_phone"])
def test_unknown_customer_country_keeps_full_shipping_validation(env, shipping_field):
    values = unknown_country_forwarder_values(env)
    values.pop(shipping_field)
    result = env.module.normalize_profile(values)
    assert shipping_field in result["missing"]


def test_unknown_customer_country_preserves_explicit_territory(env):
    result = env.module.normalize_profile(unknown_country_forwarder_values(env) | {"territory": "USA"})
    assert result["missing"] == []
    assert result["customer"]["territory"] == "USA"


def test_forwarder_requires_explicit_shipping_phone_and_customer_phone_waiver(env):
    values = forwarder_values(env)
    values.pop("shipping_phone")
    result = env.module.normalize_profile(values)
    assert "shipping_phone" in result["missing"]
    assert "mobile_no_or_phone_no" in result["missing"]
    values = forwarder_values(env)
    values.pop("contact_phone_not_provided")
    assert "mobile_no_or_phone_no" in env.module.normalize_profile(values)["missing"]


def test_forwarder_separate_billing_is_validated_independently(env):
    values = forwarder_values(env)
    values.pop("billing_address_mode")
    values["billing_address"] = {"address_line1": "11 Jalan Ampang", "city": "Kuala Lumpur",
        "country": "Malaysia", "pincode": "50450"}
    result = env.module.normalize_profile(values)
    assert result["missing"] == []
    assert result["address_policy"]["billing_mode"] == "separate"
    assert result["billing"]["country"] == "Malaysia"
    assert result["billing"]["address_type"] == "Billing"
    assert "phone" not in result["billing"]


@pytest.mark.parametrize("updates", [
    {"billing_address_mode": "same_as_shipping"},
    {"address_type": "Warehouse"},
    {"billing_address_mode": "not_provided", "billing_address": {"country": "Malaysia"}},
    {"mobile_no": "+60 123456789"},
    {"contact_phone_not_provided": "true"},
])
def test_forwarder_rejects_ambiguous_or_contradictory_purpose(env, updates):
    with pytest.raises(ValidationError):
        env.module.normalize_profile(forwarder_values(env) | updates)


def test_direct_customer_phone_cannot_be_waived_as_forwarder_phone(env):
    values = env.values | {"contact_phone_not_provided": True, "shipping_phone": "+1 2024561111"}
    values.pop("phone_no")
    with pytest.raises(ValidationError, match="只有明确货代"):
        env.module.normalize_profile(values)


def test_customer_city_requires_actual_country_and_does_not_guess_from_shipping(env):
    result = env.module.normalize_profile(env.values | {"customer_city": "Kuala Lumpur"})
    assert "customer_country" in result["missing"]
    assert result["territory_location"]["country"] == ""


def test_supplied_customer_country_does_not_inherit_shipping_city(env):
    result = env.module.normalize_profile(env.values | {"customer_country": "Malaysia"})
    assert result["territory_location"] == {"country": "Malaysia", "state": "", "city": "", "allow_country_only": True}


def test_separate_mode_requires_actual_billing_address(env):
    result = env.module.normalize_profile(env.values | {"billing_address_mode": "separate"})
    assert "billing_address" in result["missing"]


@pytest.mark.parametrize("address_type", ["直收", "直发", "客户直收"])
def test_explicit_direct_aliases_are_normalized(env, address_type):
    result = env.module.normalize_profile(env.values | {"address_type": address_type})
    assert result["address_policy"]["shipping_type"] == "Shipping"


@pytest.mark.parametrize("address_type", ["货代", "货代代收", "货代仓"])
def test_explicit_forwarder_aliases_are_normalized(env, address_type):
    result = env.module.normalize_profile(forwarder_values(env) | {"address_type": address_type})
    assert result["address_policy"]["shipping_type"] == "货代收货"


def test_customer_contact_phone_is_parsed_independently_of_direct_delivery_country(env):
    result = env.module.normalize_profile(env.values | {"customer_country": "Malaysia", "phone_no": "0123456789"})
    assert result["contact"]["phone_nos"][0]["phone"] == "+60 123456789"
    assert result["shipping"]["phone"] == "+60 123456789"
