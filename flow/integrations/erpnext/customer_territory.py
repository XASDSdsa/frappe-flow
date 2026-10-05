"""Plan and create customer city territories using native permissions/transactions."""

import frappe
import pycountry


def _key(value):
    return " ".join(str(value or "").split()).casefold()


def _state(country_code, value):
    value = " ".join(str(value or "").split())
    if not value:
        return ""
    candidates = pycountry.subdivisions.get(country_code=country_code) or []
    matches = [row for row in candidates if _key(value) in {_key(row.name), _key(row.code), _key(row.code.split("-", 1)[-1])}]
    return matches[0].name if len(matches) == 1 else value


def _find_name(name, lock):
    rows = frappe.db.sql("select name from `tabTerritory` where name = %s or territory_name = %s" + (" for update" if lock else ""), (name, name), as_dict=True)
    if len(rows) > 1:
        frappe.throw("销售区域名称不唯一，请先在地区树中核实。")
    if not rows:
        return None
    doc = frappe.get_doc("Territory", rows[0].name, for_update=lock)
    doc.check_permission("read")
    if doc.get("disabled"):
        frappe.throw("匹配的销售区域已停用，请先在地区树中核实。")
    return doc


def _root(lock):
    rows = frappe.db.sql("select name from `tabTerritory` where parent_territory is null or parent_territory = ''" + (" for update" if lock else ""), as_dict=True)
    if len(rows) != 1:
        frappe.throw("销售区域缺少唯一的根节点，请先由管理员检查地区树。")
    doc = frappe.get_doc("Territory", rows[0].name, for_update=lock)
    doc.check_permission("read")
    if not doc.get("is_group"):
        frappe.throw("销售区域根节点必须是分组，请先由管理员检查地区树。")
    return doc


def _legacy_matches(doc, shipping, country_code, state, lock):
    # Legacy bare city names have no geography fields. Only reuse when every
    # linked address provides the same country/city/state, never infer from name.
    rows = frappe.db.sql("""select distinct a.country, a.city, a.state from `tabAddress` a
        inner join `tabDynamic Link` l on l.parent = a.name and l.parenttype = 'Address'
            and l.parentfield = 'links' and l.link_doctype = 'Customer'
        inner join `tabCustomer` c on c.name = l.link_name
        where c.territory = %s""" + (" for update" if lock else ""), (doc.name,), as_dict=True)
    return bool(rows) and all(
        _key(row.country) == _key(shipping["country"])
        and _key(row.city) == _key(shipping["city"])
        and _key(_state(country_code, row.state)) == _key(state)
        for row in rows
    )


def plan_customer_territory(customer, shipping, existing=None, lock=False):
    """Read-only plan; caller holds transaction/savepoint and applies after checks.

    Explicit/existing territories are preserved. ``shipping`` is the compatible
    argument name; callers pass the normalized actual customer geography here.
    New names include country and state because native Territory names are unique.
    """
    selected = customer.get("territory") or (existing or {}).get("territory")
    if selected:
        doc = _find_name(selected, lock)
        if not doc:
            frappe.throw("指定的销售区域不存在，请选择有效区域。")
        return {"territory": doc.name, "source": "existing_customer" if (existing or {}).get("territory") == doc.name else "explicit", "will_create": []}
    if shipping.get("country_not_provided"):
        if any(shipping.get(key) for key in ("country", "state", "city")):
            frappe.throw("客户国家未提供时，不能同时填写客户国家、省州或城市。")
        return {"territory": "", "source": "customer_country_not_provided", "will_create": []}
    if lock:
        frappe.db.sql("select name from `tabDocType` where name = 'Territory' for update")
    if not shipping.get("country") or (not shipping.get("city") and not shipping.get("allow_country_only")):
        frappe.throw("自动确定销售区域需要客户实际所在国家和城市。")
    country = frappe.get_doc("Country", shipping["country"])
    country.check_permission("read")
    country_code = str(country.get("code") or "").upper()
    state = _state(country_code, shipping.get("state"))
    city = " ".join((shipping.get("city") or "").split())
    suffix = ", ".join(part for part in (state, country.name) if part)
    city_name = f"{city} ({suffix})"
    if len(city_name) > 140:
        frappe.throw("城市、省州和国家合并后超过销售区域名称上限，请核实地址简称或明确选择已有销售区域。")
    parent = _find_name(country.name, lock)
    root = _root(lock)
    planned = []
    if parent:
        if parent.name == root.name or parent.parent_territory != root.name:
            frappe.throw("同名国家销售区域不在根节点下，请由管理员确认国家层级，未移动任何地区。")
        if city and not parent.get("is_group"):
            frappe.throw(f"国家销售区域 {country.name} 目前不是分组，请管理员先转换为分组后再建立城市。")
        parent_name = parent.name
    else:
        parent_name = country.name
        planned.append({"territory_name": country.name, "parent_territory": root.name, "is_group": 1})
    if not city:
        if planned:
            frappe.has_permission("Territory", "create", throw=True)
        return {"territory": parent_name, "source": "customer_country", "country": country.name,
            "city": "", "state": state, "will_create": planned}
    current = _find_name(city_name, lock)
    if current:
        if current.parent_territory != parent_name or current.get("is_group"):
            frappe.throw("同名城市销售区域的国家归属或节点类型不一致，请先核实地区树。")
        return {"territory": current.name, "source": "shipping_city", "country": country.name, "city": city, "state": state, "will_create": []}
    legacy = _find_name(city, lock)
    if legacy and parent and legacy.parent_territory == parent.name and not legacy.get("is_group") and _legacy_matches(legacy, shipping, country_code, state, lock):
        return {"territory": legacy.name, "source": "verified_existing_city", "country": country.name, "city": city, "state": state, "will_create": []}
    planned.append({"territory_name": city_name, "parent_territory": parent_name, "is_group": 0})
    frappe.has_permission("Territory", "create", throw=True)
    return {"territory": city_name, "source": "shipping_city", "country": country.name, "city": city, "state": state, "will_create": planned}


def apply_customer_territory(plan):
    """Insert planned native nodes without committing or bypassing permissions."""
    changes = []
    for values in plan["will_create"]:
        doc = frappe.get_doc({"doctype": "Territory", **values}).insert()
        doc.check_permission("read")
        if doc.name != values["territory_name"] or doc.parent_territory != values["parent_territory"] or bool(doc.is_group) != bool(values["is_group"]):
            frappe.throw("销售区域创建后核验不一致，本次建档已取消。")
        changes.append({"doctype": "Territory", "name": doc.name, "action": "created"})
    return changes
