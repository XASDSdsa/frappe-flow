"""City territory identity, permissions and non-writing planning checks."""

import copy
import importlib.util
import sys
import types
from pathlib import Path

import pytest


class D(dict):
    __getattr__ = dict.get


class Doc(D):
    def __init__(self, harness, values):
        super().__init__(values)
        self.__dict__["h"] = harness

    def check_permission(self, permission):
        self.h.permission(self.doctype, permission, throw=True)

    def insert(self):
        self.check_permission("create")
        self["name"] = self["territory_name"]
        if self.name in self.h.territories:
            raise ValueError("duplicate")
        self.h.territories[self.name] = dict(self)
        self.h.writes += 1
        return self


class Harness:
    def __init__(self, monkeypatch):
        self.territories = {}
        self.evidence = []
        self.writes = self.locks = 0
        self.denied = set()
        self.add("All Territories", parent=None, group=1)
        self.add("United States", group=1)
        self.countries = {"United States": "us", "Canada": "ca"}
        frappe = types.ModuleType("frappe")
        frappe.db = types.SimpleNamespace(sql=self.sql)
        frappe.get_doc = self.get_doc
        frappe.has_permission = self.permission
        frappe.throw = lambda message: (_ for _ in ()).throw(ValueError(message))
        monkeypatch.setitem(sys.modules, "frappe", frappe)
        pycountry = types.ModuleType("pycountry")
        subdivisions = {"US": [D(name="Washington", code="US-WA"), D(name="Oregon", code="US-OR")], "CA": [D(name="Ontario", code="CA-ON")]}
        pycountry.subdivisions = types.SimpleNamespace(get=lambda country_code: subdivisions.get(country_code, []))
        monkeypatch.setitem(sys.modules, "pycountry", pycountry)
        spec = importlib.util.spec_from_file_location("customer_territory_under_test", Path(__file__).resolve().parents[2] / "flow/integrations/erpnext/customer_territory.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.shipping = {"country": "United States", "city": "Seattle", "state": "WA"}

    def add(self, name, parent="All Territories", group=0):
        self.territories[name] = {"doctype": "Territory", "name": name, "territory_name": name, "parent_territory": parent, "is_group": group}

    def permission(self, doctype, permission, throw=False):
        if (doctype, permission) in self.denied:
            if throw:
                raise PermissionError("denied")
            return False
        return True

    def get_doc(self, doctype, name=None, **kwargs):
        if isinstance(doctype, dict):
            return Doc(self, doctype)
        if doctype == "Country":
            return Doc(self, {"doctype": doctype, "name": name, "code": self.countries[name]})
        return Doc(self, self.territories[name])

    def sql(self, query, params=None, as_dict=False):
        if "for update" in query:
            self.locks += 1
        if "tabDocType" in query:
            return [D(name="Territory")]
        if "tabAddress" in query:
            return [D(row) for row in self.evidence]
        if "parent_territory is null" in query:
            return [D(name=name) for name, row in self.territories.items() if not row["parent_territory"]]
        if "name = %s or territory_name = %s" in query:
            return [D(name=name) for name in self.territories if name.casefold() == params[0].casefold()]
        raise AssertionError(query)

    def plan(self, customer=None, existing=None, lock=False):
        return self.module.plan_customer_territory(customer or {}, self.shipping, existing=existing, lock=lock)


@pytest.fixture
def harness(monkeypatch):
    return Harness(monkeypatch)


def test_preview_plans_city_and_normalizes_state_without_writes_or_locks(harness):
    before = copy.deepcopy(harness.territories)
    plan = harness.plan()
    assert plan["territory"] == "Seattle (Washington, United States)"
    assert plan["will_create"] == [{"territory_name": plan["territory"], "parent_territory": "United States", "is_group": 0}]
    assert harness.territories == before
    assert harness.writes == harness.locks == 0


def test_reuses_city_case_insensitively_and_state_abbreviation(harness):
    name = "Seattle (Washington, United States)"
    harness.add(name, "United States")
    harness.shipping["city"] = "seattle"
    plan = harness.plan()
    assert plan["territory"] == name
    assert not plan["will_create"]


def test_missing_country_is_planned_before_city_then_native_insert(harness):
    harness.shipping = {"country": "Canada", "city": "Toronto", "state": "ON"}
    plan = harness.plan(lock=True)
    assert [row["territory_name"] for row in plan["will_create"]] == ["Canada", "Toronto (Ontario, Canada)"]
    changes = harness.module.apply_customer_territory(plan)
    assert len(changes) == harness.writes == 2
    assert harness.territories[plan["territory"]]["parent_territory"] == "Canada"
    assert harness.territories["Canada"]["parent_territory"] == "All Territories"
    assert harness.locks > 0


def test_leaf_country_is_not_silently_modified(harness):
    harness.territories["United States"]["is_group"] = 0
    with pytest.raises(ValueError, match="转换为分组"):
        harness.plan()
    assert harness.writes == 0


def test_create_permission_required_but_not_write_permission(harness):
    harness.denied.add(("Territory", "write"))
    plan = harness.plan()
    harness.module.apply_customer_territory(plan)
    assert harness.writes == 1
    harness.shipping["city"] = "Tacoma"
    harness.denied.add(("Territory", "create"))
    with pytest.raises(PermissionError):
        harness.plan()
    assert harness.writes == 1


def test_existing_territory_remains_even_when_shipping_country_changes(harness):
    harness.shipping = {"country": "Canada", "city": "Toronto", "state": "ON"}
    plan = harness.plan(existing={"territory": "United States"})
    assert plan["territory"] == "United States"
    assert plan["source"] == "existing_customer"
    assert not plan["will_create"]


def test_explicit_territory_remains_selected(harness):
    harness.add("Special Region", group=0)
    plan = harness.plan({"territory": "Special Region"})
    assert plan["territory"] == "Special Region"
    assert not plan["will_create"]


def test_bare_city_in_other_country_does_not_collide(harness):
    harness.add("Canada", group=1)
    harness.add("Seattle", "Canada")
    plan = harness.plan()
    assert plan["territory"] == "Seattle (Washington, United States)"
    assert len(plan["will_create"]) == 1


def test_wrong_parent_for_qualified_city_blocks_creation(harness):
    harness.add("Canada", group=1)
    harness.add("Seattle (Washington, United States)", "Canada")
    with pytest.raises(ValueError, match="归属"):
        harness.plan()
    assert harness.writes == 0


def test_legacy_city_only_reused_with_matching_address_evidence(harness):
    harness.add("Seattle", "United States")
    assert harness.plan()["territory"] == "Seattle (Washington, United States)"
    harness.evidence = [{"country": "United States", "city": "Seattle", "state": "Washington"}]
    plan = harness.plan()
    assert plan["territory"] == "Seattle"
    assert not plan["will_create"]
    harness.evidence.append({"country": "United States", "city": "Seattle", "state": "Oregon"})
    assert harness.plan()["territory"] == "Seattle (Washington, United States)"


def test_country_name_at_wrong_tree_level_is_not_moved(harness):
    harness.territories["United States"]["parent_territory"] = "Other Region"
    with pytest.raises(ValueError, match="国家层级"):
        harness.plan()
    assert harness.writes == 0


def test_known_customer_country_without_city_uses_country_territory(harness):
    harness.countries["Malaysia"] = "my"
    harness.shipping = {"country": "Malaysia", "city": "", "state": "", "allow_country_only": True}
    plan = harness.plan()
    assert plan["territory"] == "Malaysia"
    assert plan["source"] == "customer_country"
    assert plan["will_create"] == [{"territory_name": "Malaysia", "parent_territory": "All Territories", "is_group": 1}]
    assert harness.writes == 0
    harness.module.apply_customer_territory(plan)
    assert harness.writes == 1
    assert "China" not in harness.territories
    assert harness.plan()["will_create"] == []


def test_known_customer_country_can_reuse_country_leaf_without_changing_it(harness):
    harness.shipping = {"country": "United States", "allow_country_only": True}
    harness.territories["United States"]["is_group"] = 0
    plan = harness.plan()
    assert plan["territory"] == "United States"
    assert not plan["will_create"]
    assert not harness.territories["United States"]["is_group"]


def test_missing_country_never_creates_country_only_territory(harness):
    harness.shipping = {"city": "", "allow_country_only": True}
    with pytest.raises(ValueError, match="客户实际所在国家"):
        harness.plan()
    assert harness.writes == 0


def test_missing_city_without_country_only_permission_remains_missing(harness):
    harness.shipping = {"country": "United States"}
    with pytest.raises(ValueError, match="客户实际所在国家"):
        harness.plan()


def test_country_only_creation_obeys_create_permission(harness):
    harness.shipping = {"country": "Canada", "allow_country_only": True}
    harness.denied.add(("Territory", "create"))
    with pytest.raises(PermissionError):
        harness.plan()
    assert harness.writes == 0


def test_existing_territory_is_not_overwritten_by_country_only_location(harness):
    harness.shipping = {"country": "Canada", "allow_country_only": True}
    plan = harness.plan(existing={"territory": "United States"})
    assert plan["territory"] == "United States"
    assert not plan["will_create"]


@pytest.mark.parametrize("lock", [False, True])
def test_explicit_unknown_country_skips_all_database_reads_and_creation(harness, monkeypatch, lock):
    harness.shipping = {"country": "", "city": "", "state": "", "allow_country_only": True,
        "country_not_provided": True}
    def unexpected_read(*args, **kwargs):
        raise AssertionError("Unknown customer country must not query geography or territory roots")
    monkeypatch.setattr(harness.module.frappe.db, "sql", unexpected_read)
    monkeypatch.setattr(harness.module.frappe, "get_doc", unexpected_read)
    plan = harness.plan(lock=lock)
    assert plan == {"territory": "", "source": "customer_country_not_provided", "will_create": []}
    assert harness.module.apply_customer_territory(plan) == []
    assert harness.writes == harness.locks == 0


@pytest.mark.parametrize(("customer", "existing", "source"), [
    ({}, {"territory": "United States"}, "existing_customer"),
    ({"territory": "United States"}, None, "explicit"),
])
def test_unknown_country_preserves_existing_or_explicit_territory(harness, customer, existing, source):
    harness.shipping = {"country": "", "city": "", "state": "", "country_not_provided": True}
    harness.denied.add(("Country", "read"))
    harness.denied.add(("Territory", "create"))
    del harness.territories["All Territories"]
    plan = harness.plan(customer, existing=existing, lock=True)
    assert plan == {"territory": "United States", "source": source, "will_create": []}
    assert harness.writes == 0


@pytest.mark.parametrize("field", ["country", "state", "city"])
def test_unknown_country_planner_rejects_conflicting_geography(harness, field):
    harness.shipping = {"country_not_provided": True, field: "conflicting geography"}
    with pytest.raises(ValueError, match="不能同时填写"):
        harness.plan()
    assert harness.writes == harness.locks == 0
