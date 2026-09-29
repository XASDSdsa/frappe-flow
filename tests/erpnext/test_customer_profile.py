"""Focused customer-tool transaction/identity checks; no server or network."""

import copy
import importlib.util
import sys
import types
from pathlib import Path

import pytest


class D(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__


class Doc:
	def __init__(self, harness, values):
		object.__setattr__(self, "h", harness)
		object.__setattr__(self, "values", copy.deepcopy(values))
		for key in ("links", "email_ids", "phone_nos"):
			if key in self.values:
				self.values[key] = [D(row) for row in self.values[key]]

	def __getattr__(self, key):
		return self.values.get(key)

	def __setattr__(self, key, value):
		self.values[key] = value

	def get(self, key, default=None):
		return self.values.get(key, default)

	def set(self, key, value):
		self.values[key] = [D(row) for row in value] if isinstance(value, list) else value

	def as_dict(self):
		return copy.deepcopy(self.values)

	def update(self, values):
		for key, value in values.items():
			self.set(key, value)

	def check_permission(self, perm):
		self.h.permission(self.doctype, perm, doc=self, throw=True)

	def insert(self):
		self.check_permission("create")
		self.name = f"{self.doctype}-{len(self.h.data[self.doctype]) + 1}"
		if self.doctype == "Customer":
			# Native make_contact first parses Individual.customer_name, then only
			# overrides truthy first_name/last_name. A full first_name input used to
			# duplicate the parsed middle and last names; model that real behavior.
			names = self.customer_name.split() if self.customer_type == "Individual" else []
			contact = Doc(self.h, {"doctype": "Contact", "first_name": self.first_name or (names[0] if names else ""), "middle_name": " ".join(names[1:-1]) if len(names) > 2 else "", "last_name": self.last_name or (names[-1] if len(names) > 1 else ""), "company_name": self.customer_name if self.customer_type != "Individual" else "", "links": [{"link_doctype": "Customer", "link_name": self.name}], "email_ids": [{"email_id": self.email_id, "is_primary": 1}] if self.email_id else [], "phone_nos": [{"phone": self.mobile_no, "is_primary_mobile_no": 1}] if self.mobile_no else [], "is_primary_contact": 1}).insert()
			self.h.native_contact_name = contact.full_name
			self.customer_primary_contact = contact.name
			if self.address_line1:
				address = Doc(self.h, {"doctype": "Address", **{key: self.get(key) for key in ("address_line1", "address_line2", "city", "state", "country", "pincode")}, "address_type": "Billing", "links": [{"link_doctype": "Customer", "link_name": self.name}], "is_primary_address": 1, "is_shipping_address": 1}).insert()
				self.customer_primary_address = address.name
		if self.doctype == "Address":
			self.h.address_insert_types.append(self.address_type)
		self._persist()
		if self.h.fail_after == self.doctype:
			raise RuntimeError("injected after native insert")
		return self

	def save(self):
		self.check_permission("write")
		self._persist()
		return self

	def db_set(self, values):
		self.update(values)
		self._persist()

	def _persist(self):
		if self.doctype == "Contact":
			self.full_name = " ".join(self.get(key) for key in ("first_name", "middle_name", "last_name") if self.get(key))
			self.email_id = next((row.email_id for row in self.get("email_ids", []) if row.is_primary), "")
			self.mobile_no = next((row.phone for row in self.get("phone_nos", []) if row.is_primary_mobile_no), "")
			self.phone = next((row.phone for row in self.get("phone_nos", []) if row.is_primary_phone), "")
		self.h.data[self.doctype][self.name] = self.as_dict()
		self.h.writes += 1

	def reload(self):
		self.update(self.h.data[self.doctype][self.name])
		return self


class Harness:
	def __init__(self, monkeypatch):
		self.data = {"Customer": {}, "Contact": {}, "Address": {}, "Territory": {}}
		self.denied = set()
		self.writes = 0
		self.locks = 0
		self.fail_after = None
		self.saved = {}
		self.address_insert_types = []
		self.profile = {"customer": {"customer_name": "Example Billiards", "customer_type": "Company", "customer_group": "Commercial", "territory": "United States", "default_currency": "USD"}, "contact": {"first_name": "Alice", "company_name": "Example Billiards", "email_ids": [{"email_id": "alice@example.com", "is_primary": 1}], "phone_nos": [{"phone": "+14155552671", "is_primary_mobile_no": 1, "is_primary_phone": 0}]}, "shipping": {"address_type": "Shipping", "address_line1": "1 Main Street", "city": "San Francisco", "state": "CA", "country": "United States", "pincode": "94102", "phone": "+14155552671", "email_id": "alice@example.com"}, "billing": None, "missing": [], "warnings": [], "defaults_used": {}}
		self.profile["address_policy"] = {"shipping_type": "Shipping", "billing_mode": "same_as_shipping"}
		self.profile["territory_location"] = {"country": "United States", "state": "CA", "city": "San Francisco", "allow_country_only": False}
		frappe = types.ModuleType("frappe")
		frappe.PermissionError = PermissionError
		frappe.ValidationError = ValueError
		frappe.get_doc = self.get_doc
		frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
		frappe.has_permission = self.permission
		frappe.throw = lambda message: (_ for _ in ()).throw(ValueError(message))
		frappe.clear_document_cache = lambda *args: None
		frappe.db = types.SimpleNamespace(sql=self.sql, exists=lambda dt, name: name in self.data[dt], savepoint=self.savepoint, rollback=self.rollback)
		monkeypatch.setitem(sys.modules, "frappe", frappe)
		address_module = types.ModuleType("frappe.contacts.doctype.address.address")
		address_module.get_address_display = lambda name: self.data["Address"][name]["address_line1"]
		monkeypatch.setitem(sys.modules, address_module.__name__, address_module)
		flow_tool = types.ModuleType("flow.lib.tool")
		def tool(function, **metadata):
			function.tool_metadata = metadata
			return function
		flow_tool.tool = tool
		monkeypatch.setitem(sys.modules, flow_tool.__name__, flow_tool)
		review = types.ModuleType("flow.integrations.erpnext.customer_profile_review")
		review.customer_confirmation_prompt = lambda args: "review"
		monkeypatch.setitem(sys.modules, review.__name__, review)
		validation = types.ModuleType("flow.integrations.erpnext.customer_profile_validation")
		validation.INPUT_FIELDS = {"customer_name", "customer_type", "address_line1", "city", "country", "email_id", "mobile_no", "contact_name", "customer_group", "default_currency", "billing_address"}
		validation.normalize_profile = lambda values, existing=None: copy.deepcopy(self.profile)
		monkeypatch.setitem(sys.modules, validation.__name__, validation)
		territory = types.ModuleType("flow.integrations.erpnext.customer_territory")
		territory.plan_customer_territory = lambda customer, shipping, existing=None, lock=False: copy.deepcopy(self.territory_plan or {"territory": customer.get("territory") or (existing or {}).get("territory") or "Test City (Test State, Test Country)", "source": "shipping_city", "will_create": []})
		territory.apply_customer_territory = self.apply_territory
		self.territory_plan = None
		monkeypatch.setitem(sys.modules, territory.__name__, territory)
		spec = importlib.util.spec_from_file_location("customer_profile_under_test", Path(__file__).resolve().parents[2] / "flow/integrations/erpnext/customer_profile.py")
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)

	def apply_territory(self, plan):
		changes = []
		for values in plan["will_create"]:
			doc = Doc(self, {"doctype": "Territory", **values}).insert()
			changes.append({"doctype": "Territory", "name": doc.name, "action": "created"})
		return changes

	def get_doc(self, doctype, name=None, **kwargs):
		return Doc(self, doctype if isinstance(doctype, dict) else self.data[doctype][name])

	def permission(self, doctype, perm, doc=None, throw=False):
		allowed = (doctype, perm) not in self.denied and (doctype, getattr(doc, "name", None), perm) not in self.denied
		if throw and not allowed:
			raise PermissionError("denied")
		return allowed

	def savepoint(self, name):
		self.saved[name] = copy.deepcopy(self.data)

	def rollback(self, save_point):
		self.data = copy.deepcopy(self.saved[save_point])

	def sql(self, query, args=None, as_dict=False):
		if "for update" in query:
			self.locks += 1
		if "tabDocType" in query:
			return [{"name": "Customer"}]
		if "tabCustomer" in query:
			return [D(name=name) for name, row in self.data["Customer"].items() if row["customer_name"].casefold() == args[0].casefold()]
		if "tabDynamic Link" in query:
			doctype, customer_id = args
			return [D(name=name) for name, row in self.data[doctype].items() if any(link["link_doctype"] == "Customer" and link["link_name"] == customer_id for link in row.get("links", []))]
		raise AssertionError(query)

	def call(self, **kwargs):
		return self.module.create_or_reuse_customer_profile(customer_name="Example Billiards", **kwargs)


@pytest.fixture
def harness(monkeypatch):
	return Harness(monkeypatch)


def test_existing_responsibility_cannot_be_overwritten_by_profile_completion(harness):
	harness.profile["customer"]["account_manager"] = "original@example.com"
	created = harness.call()
	assert created["status"] == "created", created
	before = copy.deepcopy(harness.data)
	harness.profile["customer"]["account_manager"] = "another@example.com"
	result = harness.call(customer_id=created["customer_id"])
	assert result["status"] == "needs_input", result
	assert any(row["field"] == "account_manager" for row in result["conflicts"])
	assert harness.data == before


def test_new_native_customer_does_not_need_customer_write(harness):
	harness.profile["customer"]["account_manager"] = "agent@example.com"
	harness.denied.add(("Customer", "write"))
	result = harness.call()
	assert result["status"] == "created", result
	assert result["verified"]
	assert result["customer"]["account_manager"] == "agent@example.com"
	assert result["contact"]["email_id"] == "alice@example.com"
	assert result["contact"]["mobile_no"] == "+14155552671"
	assert result["shipping_address"]["address_type"] == "Shipping"
	assert len(harness.data["Customer"]) == len(harness.data["Contact"]) == len(harness.data["Address"]) == 1


def test_native_failure_rolls_back_all_created_records(harness):
	harness.data["Customer"]["unrelated"] = {"doctype": "Customer", "name": "unrelated", "customer_name": "Other"}
	before = copy.deepcopy(harness.data)
	harness.fail_after = "Customer"
	result = harness.call()
	assert result["status"] == "error" and result["rolled_back"]
	assert harness.data == before
	assert harness.writes > 0


def test_permission_preflight_happens_before_native_writes(harness):
	harness.denied.add(("Address", "write"))
	result = harness.call()
	assert result["status"] == "error" and result["rolled_back"]
	assert harness.writes == 0


def test_dry_run_no_writes_or_locks(harness):
	result = harness.call(dry_run=True)
	assert result["status"] == "preview"
	assert harness.writes == harness.locks == 0


def test_missing_fields_no_writes(harness):
	harness.profile["missing"] = ["pincode", "contact_name"]
	result = harness.call()
	assert result["status"] == "needs_input"
	assert result["missing"] == ["pincode", "contact_name"]
	assert harness.writes == 0


def test_missing_currency_does_not_require_territory_selection(harness):
	harness.profile["missing"] = ["default_currency"]
	harness.profile["customer"].pop("territory")
	result = harness.call(dry_run=True)
	assert result["missing"] == ["default_currency"]
	automatic = result["automatic_fields"]["territory"]
	assert automatic["mode"] == "match_or_create_customer_location"
	assert automatic["city"] == "San Francisco"
	assert not automatic["requires_user_selection"]
	assert not automatic["requires_separate_confirmation"]
	assert harness.writes == harness.locks == 0


def test_preview_directs_onboarding_without_a_separate_territory_approval(harness):
	harness.profile["customer"].pop("territory")
	result = harness.call(dry_run=True)
	assert result["status"] == "preview"
	assert result["next_action"] == "create_or_reuse_customer_profile"
	assert not result["profile"]["automatic_fields"]["territory"]["requires_separate_confirmation"]
	assert harness.writes == harness.locks == 0


def test_same_name_requires_explicit_customer_id(harness):
	created = harness.call()
	writes = harness.writes
	result = harness.call(confirm_existing_customer=True)
	assert result["status"] == "needs_input" and result["missing"] == ["customer_id"]
	assert result["candidates"][0]["customer_id"] == created["customer_id"]
	assert harness.writes == writes


def test_hidden_same_name_does_not_leak_or_duplicate(harness):
	created = harness.call()
	harness.denied.add(("Customer", created["customer_id"], "read"))
	result = harness.call()
	assert result["status"] == "needs_input"
	assert result["candidates"] == []
	assert len(harness.data["Customer"]) == 1


def test_existing_identical_needs_no_write_or_create_permissions(harness):
	created = harness.call()
	before = copy.deepcopy(harness.data)
	for dt in harness.data:
		for perm in ("create", "write"):
			harness.denied.add((dt, perm))
	result = harness.call(customer_id=created["customer_id"])
	assert result["status"] == "reused", result
	assert harness.data == before


def test_existing_nonempty_fields_are_never_overwritten(harness):
	created = harness.call()
	before = copy.deepcopy(harness.data)
	harness.profile["customer"]["territory"] = "China"
	result = harness.call(customer_id=created["customer_id"])
	assert result["status"] == "needs_input"
	assert result["conflicts"][0]["field"] == "territory"
	assert harness.data == before


def test_disabled_customer_blocks_supplement(harness):
	created = harness.call()
	harness.data["Customer"][created["customer_id"]]["disabled"] = 1
	writes = harness.writes
	result = harness.call(customer_id=created["customer_id"])
	assert result["status"] == "needs_input"
	assert harness.writes == writes


def test_address_unit_difference_creates_nonprimary_supplement(harness):
	created = harness.call()
	original = copy.deepcopy(harness.data["Customer"][created["customer_id"]])
	old_address = created["shipping_address"]["name"]
	harness.profile["shipping"]["address_line2"] = "Unit 2"
	result = harness.call(customer_id=created["customer_id"])
	assert result["status"] == "updated", result
	assert len(harness.data["Address"]) == 2
	assert harness.data["Customer"][created["customer_id"]] == original
	assert harness.data["Address"][old_address]["is_shipping_address"] == 1
	new = harness.data["Address"][result["shipping_address"]["name"]]
	assert not new["is_primary_address"] and not new["is_shipping_address"]
	retry = harness.call(customer_id=created["customer_id"])
	assert retry["status"] == "reused"


def test_different_billing_address_gets_own_role(harness):
	harness.profile["billing"] = {**harness.profile["shipping"], "address_type": "Billing", "address_line1": "2 Accounts Street"}
	harness.profile["address_policy"]["billing_mode"] = "separate"
	result = harness.call()
	assert result["status"] == "created", result
	assert result["customer"]["primary_address"] == result["billing_address"]["name"]
	assert result["shipping_address"]["name"] != result["billing_address"]["name"]
	assert len(harness.data["Address"]) == 2


def test_all_supplied_phone_rows_are_preserved(harness):
	harness.profile["contact"]["phone_nos"] = [
		{"phone": "+442079460001", "is_primary_mobile_no": 0, "is_primary_phone": 1},
		{"phone": "+442079460002", "is_primary_mobile_no": 0, "is_primary_phone": 0},
	]
	result = harness.call()
	assert result["status"] == "created", result
	contact = harness.data["Contact"][result["contact"]["name"]]
	assert {row["phone"] for row in contact["phone_nos"]} == {"+442079460001", "+442079460002"}
	assert not contact["mobile_no"]


def test_native_passive_contact_is_reusable_not_disabled(harness):
    created = harness.call()
    person = harness.data['Contact'][created['contact']['name']]
    person['status'] = 'Passive'
    result = harness.call(customer_id=created['customer_id'])
    assert result['status'] == 'reused', result
    assert len(harness.data['Contact']) == 1


def test_missing_contact_email_is_filled_without_duplicate(harness):
    created = harness.call()
    person = harness.data['Contact'][created['contact']['name']]
    person['email_ids'] = []
    person['email_id'] = ''
    harness.data['Customer'][created['customer_id']]['email_id'] = ''
    result = harness.call(customer_id=created['customer_id'])
    assert result['status'] == 'updated', result
    assert len(harness.data['Contact']) == 1
    assert result['contact']['email_id'] == 'alice@example.com'
    assert any(change['doctype'] == 'Contact' and change['action'] == 'filled_empty_fields' for change in result['changes'])


def test_primary_phone_conflict_never_creates_duplicate_contact(harness):
    created = harness.call()
    before = copy.deepcopy(harness.data)
    harness.profile['contact']['phone_nos'][0]['phone'] = '+14155552672'
    result = harness.call(customer_id=created['customer_id'])
    assert result['status'] == 'needs_input', result
    assert harness.data == before


def test_legacy_formatted_phone_reuses_existing_contact(harness):
    created = harness.call()
    harness.data['Contact'][created['contact']['name']]['phone_nos'][0]['phone'] = '+1 415 555 2671'
    result = harness.call(customer_id=created['customer_id'])
    assert result['status'] == 'reused', result
    assert len(harness.data['Contact']) == 1


def test_preview_reports_planned_territory_without_writes(harness):
    harness.territory_plan = {"territory": "Seattle (Washington, United States)", "source": "shipping_city", "will_create": [{"territory_name": "Seattle (Washington, United States)", "parent_territory": "United States", "is_group": 0}]}
    result = harness.call(dry_run=True)
    assert result["status"] == "preview", result
    assert result["profile"]["territory_plan"] == harness.territory_plan
    assert result["profile"]["customer"]["territory"] == harness.territory_plan["territory"]
    assert harness.writes == harness.locks == 0


def test_customer_failure_rolls_back_new_country_and_city(harness):
    harness.territory_plan = {"territory": "Seattle (Washington, United States)", "source": "shipping_city", "will_create": [{"territory_name": "United States", "parent_territory": "All Territories", "is_group": 1}, {"territory_name": "Seattle (Washington, United States)", "parent_territory": "United States", "is_group": 0}]}
    before = copy.deepcopy(harness.data)
    harness.fail_after = "Customer"
    result = harness.call()
    assert result["status"] == "error" and result["rolled_back"], result
    assert harness.data == before
    assert harness.writes > 2


def test_customer_permission_denial_does_not_create_planned_city(harness):
    harness.territory_plan = {"territory": "Seattle (Washington, United States)", "source": "shipping_city", "will_create": [{"territory_name": "Seattle (Washington, United States)", "parent_territory": "United States", "is_group": 0}]}
    harness.denied.add(("Customer", "create"))
    result = harness.call()
    assert result["status"] == "error" and result["rolled_back"], result
    assert harness.writes == 0


@pytest.mark.parametrize("name", ["Charles Timothy Intong", "Mary Jane Ann Smith", "Alice Smith", "Alice", "王小明"])
def test_individual_native_name_split_is_not_duplicated(harness, name):
	harness.profile["customer"].update(customer_name=name, customer_type="Individual", customer_group="Individual")
	harness.profile["contact"].update(first_name=name, company_name="")
	result = harness.module.create_or_reuse_customer_profile(customer_name=name)
	assert result["status"] == "created", result
	assert result["verified"]
	assert harness.native_contact_name == name
	assert result["contact"]["full_name"] == name
	person = harness.data["Contact"][result["contact"]["name"]]
	parts = name.split()
	assert person["first_name"] == parts[0]
	assert person["middle_name"] == (" ".join(parts[1:-1]) if len(parts) > 2 else "")
	assert person["last_name"] == (parts[-1] if len(parts) > 1 else "")
	before = copy.deepcopy(harness.data)
	reused = harness.module.create_or_reuse_customer_profile(customer_name=name, customer_id=result["customer_id"])
	assert reused["status"] == "reused", reused
	assert harness.data == before


@pytest.mark.parametrize("contact_name", ["John Michael Doe", "Alex"])
def test_individual_different_contact_clears_native_customer_name_parts(harness, contact_name):
	harness.profile["customer"].update(customer_name="Charles Timothy Intong", customer_type="Individual", customer_group="Individual")
	harness.profile["contact"].update(first_name=contact_name, company_name="")
	result = harness.call()
	assert result["status"] == "created", result
	assert result["contact"]["full_name"] == contact_name
	person = harness.data["Contact"][result["contact"]["name"]]
	assert not person["middle_name"] and not person["last_name"]
	assert result["customer"]["customer_name"] == "Charles Timothy Intong"


def test_company_multiword_contact_keeps_full_name(harness):
	harness.profile["contact"]["first_name"] = "Alice Jane Smith"
	result = harness.call()
	assert result["status"] == "created", result
	assert result["contact"]["full_name"] == "Alice Jane Smith"
	assert harness.data["Contact"][result["contact"]["name"]]["company_name"] == "Example Billiards"


def step_map(result):
	assert all(row["label"] and row["action"] and row["reason"] for row in result["steps"])
	return {row["step"]: row for row in result["steps"]}


def test_preview_explains_checks_and_does_not_claim_saved(harness):
	result = harness.call(dry_run=True)
	steps = step_map(result)
	assert steps["validate_profile"]["status"] == "passed"
	assert steps["save_customer"]["status"] == "not_executed"
	assert steps["verify_profile"]["status"] == "not_executed"
	assert not any(row["status"] in ("saved", "rolled_back") for row in result["steps"])
	assert harness.writes == 0


def test_missing_currency_identifies_stopping_point_and_unexecuted_steps(harness):
	harness.profile["missing"] = ["default_currency"]
	result = harness.call()
	steps = step_map(result)
	assert result["failed_step"] == "validate_profile"
	assert "结算币种" in steps["validate_profile"]["reason"]
	assert steps["save_customer"]["status"] == "not_executed"
	assert harness.writes == 0


def test_failed_native_creation_reports_prior_territory_rollback(harness):
	harness.territory_plan = {"territory": "Test City", "source": "shipping_city", "will_create": [{"territory_name": "Test City", "parent_territory": "All Territories", "is_group": 0}]}
	harness.fail_after = "Customer"
	before = copy.deepcopy(harness.data)
	result = harness.call()
	steps = step_map(result)
	assert result["failed_step"] == "save_customer"
	assert steps["save_territory"]["status"] == "rolled_back"
	assert steps["save_customer"]["status"] == "failed"
	assert steps["save_contact"]["status"] == "not_executed"
	assert steps["check_permissions"]["status"] == "passed"
	assert result["next_action"] == "contact_administrator_do_not_repeat_creation"
	assert harness.data == before


@pytest.mark.parametrize("target,field,value,label", [("Contact", "middle_name", "UNEXPECTED_PRIVATE_NAME", "联系人"), ("Address", "city", "UNEXPECTED_PRIVATE_CITY", "收货地址")])
def test_readback_mismatch_reports_document_and_rolls_back_without_leaking_values(harness, monkeypatch, target, field, value, label):
	create = harness.module._new_profile
	def corrupt_after_create(profile, steps):
		result = create(profile, steps)
		doc = result[1] if target == "Contact" else result[2]
		harness.data[target][doc.name][field] = value
		return result
	monkeypatch.setattr(harness.module, "_new_profile", corrupt_after_create)
	before = copy.deepcopy(harness.data)
	result = harness.call()
	steps = step_map(result)
	assert result["failed_step"] == "verify_profile"
	assert result["verification_error"]["document"] == label
	assert ("full_name" if target == "Contact" else field) in result["verification_error"]["fields"]
	assert value not in str(result)
	assert steps["save_customer"]["status"] == steps["save_contact"]["status"] == steps["save_shipping_address"]["status"] == "rolled_back"
	assert steps["verify_profile"]["status"] == "failed"
	assert not any(row["status"] == "saved" for row in result["steps"])
	assert harness.data == before


def test_success_steps_distinguish_saved_from_unneeded_billing(harness):
	result = harness.call()
	steps = step_map(result)
	assert steps["save_customer"]["status"] == "saved"
	assert steps["save_contact"]["status"] == "saved"
	assert steps["save_billing_address"]["status"] == "not_required"
	assert steps["verify_profile"]["status"] == "passed"
	assert not any(row["status"] in ("not_executed", "running") for row in result["steps"])


def test_duplicate_check_explains_block_and_does_not_leak_hidden_name(harness):
	created = harness.call()
	harness.denied.add(("Customer", created["customer_id"], "read"))
	result = harness.call()
	assert result["failed_step"] == "resolve_customer"
	assert result["candidates"] == []
	assert "Customer-1" not in str(result)
	assert step_map(result)["save_customer"]["status"] == "not_executed"


def test_reused_profile_steps_do_not_claim_new_writes(harness):
	created = harness.call()
	result = harness.call(customer_id=created["customer_id"])
	steps = step_map(result)
	assert result["status"] == "reused"
	assert steps["save_customer"]["status"] == steps["save_contact"]["status"] == steps["save_shipping_address"]["status"] == "reused"
	assert steps["verify_profile"]["status"] == "passed"
	assert not any(row["status"] == "saved" for row in result["steps"])


def test_error_logging_failure_does_not_mask_rollback(harness, monkeypatch):
	harness.fail_after = "Customer"
	monkeypatch.setattr(harness.module.frappe, "get_traceback", lambda: "safe traceback", raising=False)
	def fail_log(**kwargs):
		raise RuntimeError("logging unavailable")
	monkeypatch.setattr(harness.module.frappe, "log_error", fail_log, raising=False)
	before = copy.deepcopy(harness.data)
	result = harness.call()
	assert result["status"] == "error"
	assert result["rolled_back"]
	assert result["failed_step"] == "save_customer"
	assert harness.data == before


def forwarder_profile(harness):
	harness.profile["customer"]["territory"] = "Malaysia"
	harness.profile["address_policy"] = {"shipping_type": "货代收货", "billing_mode": "not_provided"}
	harness.profile["territory_location"] = {"country": "Malaysia", "city": "", "state": "", "allow_country_only": True}
	harness.profile["shipping"].update(address_type="货代收货", address_title="运道仓收货人", address_line1="53 Zhonghe Road", city="Guangzhou", state="Guangdong", country="China", pincode="510545", phone="+86 13809242282", email_id="")
	harness.profile["contact"]["phone_nos"] = []
	harness.profile["contact"]["email_ids"] = []


def test_forwarder_unknown_billing_never_creates_fake_billing_or_copies_warehouse_phone(harness):
	forwarder_profile(harness)
	result = harness.call()
	assert result["status"] == "created", result
	assert not result["customer"]["primary_address"]
	assert result["billing_address"] is None
	assert not result["contact"]["mobile_no"] and not result["contact"]["phone"]
	assert result["shipping_address"]["phone"] == "+86 13809242282"
	assert result["shipping_address"]["address_title"] == "运道仓收货人"
	assert result["shipping_address"]["is_shipping_address"] == 1
	assert result["shipping_address"]["is_primary_address"] == 0
	assert harness.address_insert_types == ["货代收货"]
	assert "留空" in step_map(result)["save_billing_address"]["reason"] or "空白" in step_map(result)["save_billing_address"]["reason"]
	assert len(harness.data["Address"]) == 1


def test_forwarder_preview_uses_customer_country_only_and_shows_roles(harness, monkeypatch):
	forwarder_profile(harness)
	harness.profile["customer"].pop("territory")
	seen = []
	def plan(customer, location, **kwargs):
		seen.append(location)
		return {"territory": "Malaysia", "source": "customer_country", "will_create": []}
	monkeypatch.setattr(harness.module, "plan_customer_territory", plan)
	result = harness.call(dry_run=True)
	assert result["status"] == "preview", result
	assert seen == [harness.profile["territory_location"]]
	assert result["profile"]["automatic_fields"]["territory"]["country"] == "Malaysia"
	assert result["review_summary"]["shipping"]["label"] == "货代代收（每单核对地址）"
	assert result["review_summary"]["contact"]["phone"] == "未提供"
	assert result["review_summary"]["billing"]["address"] is None
	assert harness.writes == harness.locks == 0


def test_explicit_shared_direct_address_has_both_flags_and_truthful_native_type(harness):
	result = harness.call()
	assert result["status"] == "created", result
	assert result["customer"]["primary_address"] == result["shipping_address"]["name"] == result["billing_address"]["name"]
	assert result["shipping_address"]["is_primary_address"] == result["shipping_address"]["is_shipping_address"] == 1
	assert harness.address_insert_types == ["Shipping"]


def test_separate_billing_never_receives_shipping_preference(harness):
	forwarder_profile(harness)
	harness.profile["billing"] = {"address_type": "Billing", "address_line1": "10 Customer Road", "city": "Kuala Lumpur", "country": "Malaysia"}
	harness.profile["address_policy"]["billing_mode"] = "separate"
	result = harness.call()
	assert result["status"] == "created", result
	assert result["customer"]["primary_address"] == result["billing_address"]["name"]
	assert result["billing_address"]["is_primary_address"] == 1
	assert not result["billing_address"]["is_shipping_address"]
	assert not result["shipping_address"]["is_primary_address"]
	assert harness.address_insert_types == ["货代收货", "Billing"]


def test_existing_unknown_billing_does_not_backfill_primary_address(harness):
	forwarder_profile(harness)
	created = harness.call()
	before = copy.deepcopy(harness.data)
	result = harness.call(customer_id=created["customer_id"])
	assert result["status"] == "reused", result
	assert harness.data == before
	assert not result["customer"]["primary_address"]


def test_direct_unknown_billing_does_not_assume_shared_address(harness):
	harness.profile["address_policy"]["billing_mode"] = "not_provided"
	created = harness.call()
	assert created["status"] == "created", created
	assert not created["customer"]["primary_address"]
	assert not created["shipping_address"]["is_primary_address"]
	result = harness.call(customer_id=created["customer_id"])
	assert result["status"] == "reused", result
	assert not result["customer"]["primary_address"]


def test_missing_shipping_preference_is_filled_without_replacing_billing(harness):
	created = harness.call()
	address = harness.data["Address"][created["shipping_address"]["name"]]
	address["is_shipping_address"] = 0
	result = harness.call(customer_id=created["customer_id"])
	assert result["status"] == "updated", result
	assert result["shipping_address"]["is_shipping_address"] == 1
	assert result["customer"]["primary_address"] == created["customer"]["primary_address"]
	assert len(harness.data["Address"]) == 1


def test_existing_preferred_shipping_is_preserved_for_additional_forwarder(harness):
	created = harness.call()
	original_customer = copy.deepcopy(harness.data["Customer"][created["customer_id"]])
	harness.profile["address_policy"] = {"shipping_type": "货代收货", "billing_mode": "not_provided"}
	harness.profile["shipping"].update(address_type="货代收货", address_line1="53 Warehouse Road")
	result = harness.call(customer_id=created["customer_id"])
	assert result["status"] == "updated", result
	assert harness.data["Customer"][created["customer_id"]] == original_customer
	assert harness.data["Address"][created["shipping_address"]["name"]]["is_shipping_address"] == 1
	assert not result["shipping_address"]["is_shipping_address"]
	assert not result["shipping_address"]["is_primary_address"]


def test_existing_same_physical_address_with_different_purpose_requires_review(harness):
	created = harness.call()
	before = copy.deepcopy(harness.data)
	harness.profile["address_policy"] = {"shipping_type": "货代收货", "billing_mode": "not_provided"}
	harness.profile["shipping"]["address_type"] = "货代收货"
	result = harness.call(customer_id=created["customer_id"])
	assert result["status"] == "needs_input", result
	assert "address_type" in result["fields"]
	assert harness.data == before


def test_existing_forwarder_misclassified_as_billing_is_not_silently_preserved(harness):
	forwarder_profile(harness)
	created = harness.call()
	harness.data["Address"][created["shipping_address"]["name"]]["is_primary_address"] = 1
	before = copy.deepcopy(harness.data)
	result = harness.call(customer_id=created["customer_id"])
	assert result["status"] == "needs_input", result
	assert "is_primary_address" in result["fields"]
	assert harness.data == before


def test_preference_readback_corruption_rolls_back_new_customer(harness, monkeypatch):
	forwarder_profile(harness)
	create = harness.module._new_profile
	def wrong_flag(profile, steps):
		result = create(profile, steps)
		harness.data["Address"][result[2].name]["is_primary_address"] = 1
		return result
	monkeypatch.setattr(harness.module, "_new_profile", wrong_flag)
	before = copy.deepcopy(harness.data)
	result = harness.call()
	assert result["status"] == "error", result
	assert result["failed_step"] == "verify_profile"
	assert result["verification_error"]["fields"] == ["is_primary_address"]
	assert harness.data == before


def test_address_failure_rolls_back_customer_and_contact(harness):
	forwarder_profile(harness)
	harness.fail_after = "Address"
	before = copy.deepcopy(harness.data)
	result = harness.call()
	assert result["status"] == "error", result
	assert result["failed_step"] == "save_shipping_address"
	assert harness.data == before


def test_write_tool_has_one_structured_confirmation_but_preview_calls_readonly_core(harness):
	metadata = harness.module.create_or_reuse_customer_profile.tool_metadata
	assert metadata["requires_confirmation"] is True
	assert callable(metadata["confirm_prompt"])
	result = harness.module.preview_customer_profile(customer_name="Example Billiards", dry_run=False)
	assert result["status"] == "preview", result
	assert harness.writes == 0


def test_not_provided_review_preserves_existing_billing_instead_of_claiming_it_cleared(harness):
	created = harness.call()
	harness.profile["address_policy"]["billing_mode"] = "not_provided"
	result = harness.call(customer_id=created["customer_id"], dry_run=True)
	assert result["status"] == "preview", result
	assert result["review_summary"]["billing"]["preserves_existing"]
	assert "保留" in result["review_summary"]["billing"]["label"]
	assert result["profile"]["existing_address_preferences"]["customer_primary_address"] == created["shipping_address"]["name"]


def unknown_country_profile(harness):
	forwarder_profile(harness)
	harness.profile["customer"].pop("territory", None)
	harness.profile["territory_location"] = {"country": "", "state": "", "city": "", "country_not_provided": True}
	harness.territory_plan = {"territory": "", "source": "customer_country_not_provided", "will_create": []}


def test_unknown_country_creates_without_region_and_can_supplement_later(harness):
	unknown_country_profile(harness)
	preview = harness.call(dry_run=True, customer_country_not_provided=True)
	assert preview["status"] == "preview", preview
	assert preview["profile"]["automatic_fields"]["territory"]["mode"] == "leave_blank_customer_country_unknown"
	assert harness.writes == 0
	result = harness.call(customer_country_not_provided=True)
	assert result["status"] == "created", result
	assert not result["customer"]["territory"]
	assert not harness.data["Territory"]
	assert "留空" in result["review_summary"]["territory_label"]
	assert step_map(result)["save_territory"]["status"] == "not_required"
	harness.territory_plan = {"territory": "Malaysia", "source": "customer_country", "will_create": []}
	updated = harness.call(customer_id=result["customer_id"])
	assert updated["status"] == "updated", updated
	assert updated["customer"]["territory"] == "Malaysia"
	assert len(harness.data["Customer"]) == len(harness.data["Address"]) == 1


def test_unknown_country_default_territory_injection_rolls_back(harness, monkeypatch):
	unknown_country_profile(harness)
	create = harness.module._new_profile
	def wrong_territory(profile, steps):
		result = create(profile, steps)
		harness.data["Customer"][result[0].name]["territory"] = "China"
		return result
	monkeypatch.setattr(harness.module, "_new_profile", wrong_territory)
	before = copy.deepcopy(harness.data)
	result = harness.call(customer_country_not_provided=True)
	assert result["status"] == "error", result
	assert result["verification_error"]["fields"] == ["territory"]
	assert harness.data == before
