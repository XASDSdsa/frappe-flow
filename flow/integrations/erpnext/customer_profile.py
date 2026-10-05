"""Detailed, permission-aware and atomic customer profiles for Flow."""

import re
from functools import wraps
from typing import Annotated
from uuid import uuid4

import frappe

from flow.integrations.erpnext.customer_profile_validation import INPUT_FIELDS, normalize_profile
from flow.integrations.erpnext.customer_territory import apply_customer_territory, plan_customer_territory


ADDRESS_IDENTITY = ("address_type", "address_line1", "address_line2", "city", "state", "county", "country", "pincode")
ADDRESS_FLAGS = ("is_primary_address", "is_shipping_address")
CONTACT_IDENTITY = ("first_name", "middle_name", "last_name", "company_name")
CUSTOMER_FIELDS = ("customer_name", "customer_type", "customer_group", "territory", "default_currency", "default_price_list", "payment_terms", "tax_id", "tax_category", "language", "website", "customer_details", "industry", "market_segment", "account_manager")


class ProfileInputError(Exception):
	def __init__(self, message, **details):
		super().__init__(message)
		self.details = details


class ProfileVerificationError(Exception):
	def __init__(self, document, fields, reason):
		super().__init__(f"{document}回读核验失败：{reason}。本次修改已回滚，请联系管理员核查，不要重复创建。")
		self.details = {"document": document, "fields": fields, "reason": reason}


STEP_LABELS = {
	"validate_arguments": "检查调用参数", "resolve_customer": "核对客户及重复档案",
	"validate_profile": "检查必填资料与格式", "plan_territory": "自动匹配销售地区",
	"check_permissions": "检查权限与已有资料冲突", "save_territory": "保存销售地区",
	"save_customer": "保存客户档案", "save_contact": "保存联系人",
	"save_shipping_address": "保存收货地址", "save_billing_address": "保存账单地址",
	"verify_profile": "回读核验保存结果",
}
FIELD_LABELS = {
	"customer_name": "客户名称", "customer_type": "客户类型", "customer_group": "客户分组",
	"territory": "销售地区", "default_currency": "结算币种", "account_manager": "客户负责人",
	"contact_name": "联系人姓名", "first_name": "名字", "middle_name": "中间名", "last_name": "姓氏",
	"company_name": "公司名称", "full_name": "联系人姓名", "email_id": "邮箱", "mobile_no": "手机号",
	"phone": "电话", "phone_no": "电话", "email_ids": "邮箱及主邮箱标记", "phone_nos": "电话及主号码标记",
	"address_type": "地址用途", "address_line1": "详细地址", "address_line2": "补充地址",
	"city": "城市", "state": "省州", "county": "县区", "country": "国家", "pincode": "邮编",
	"customer_primary_contact": "首选联系人", "customer_primary_address": "首选地址",
	"designation": "联系人职务", "department": "联系人部门", "links": "客户关联",
	"billing_address_mode": "发票地址安排", "customer_country": "客户所在国家", "customer_city": "客户所在城市",
	"customer_country_not_provided": "客户国家暂未提供",
	"shipping_phone": "收货电话", "shipping_contact_name": "收货人", "is_primary_address": "首选开票地址",
	"is_shipping_address": "首选送货地址",
}


class ProfileSteps:
	"""Report actual progress without treating rolled-back writes as successes."""
	def __init__(self):
		self.rows = []
		self.current = None

	def begin(self, key, action):
		self.current = {"step": key, "label": STEP_LABELS[key], "status": "running", "action": action, "reason": "正在执行。"}
		self.rows.append(self.current)

	def done(self, reason, status="passed"):
		self.current.update(status=status, reason=reason)

	def fail(self, reason, rolled_back=False):
		failed_step = self.current["step"]
		self.current.update(status="failed", reason=reason)
		if rolled_back:
			for row in self.rows:
				if row["status"] == "saved":
					row.update(status="rolled_back", reason=row["reason"] + " 后续步骤失败，本次写入已全部回滚，未保留修改。")
			self.current["rolled_back"] = True
		return failed_step

	def result(self, result):
		seen = {row["step"] for row in self.rows}
		preview = result["status"] == "preview"
		reason = "本次仅预检，尚未执行写入或保存后回读。" if preview else "前序步骤未通过，因此未执行。"
		rows = list(self.rows)
		for key, label in STEP_LABELS.items():
			if key not in seen:
				rows.append({"step": key, "label": label, "status": "not_executed", "action": label, "reason": reason})
		return {**result, "steps": rows}


def _key(value):
	return str(value or "").strip().casefold()


def _allowed(doctype, values):
	meta = frappe.get_meta(doctype)
	return {key: value for key, value in values.items() if meta.has_field(key)}


def _linked(doc, doctype, name):
	return any(row.link_doctype == doctype and row.link_name == name for row in doc.get("links", []))


def _check_enabled(customer):
	if customer.get("disabled") or _key(customer.get("is_frozen")) in ("yes", "1", "true"):
		raise ProfileInputError("该客户已停用或冻结，请先在客户档案中核实。", customer_id=customer.name)


def _resolve_customer(customer_id, customer_name, lock):
	if customer_id:
		if not frappe.db.exists("Customer", customer_id):
			raise ProfileInputError("客户编号不存在，请核实客户编号。", missing=["customer_id"])
		customer = frappe.get_doc("Customer", customer_id, for_update=lock)
		customer.check_permission("read")
		_check_enabled(customer)
		if customer_name and _key(customer.customer_name) != _key(customer_name):
			raise ProfileInputError("客户编号与客户名称不一致，请确认本次操作对象。", missing=["customer_id"], customer_id=customer.name)
		return customer
	if not customer_name:
		return None
	# A current locking read sees a preceding tool transaction after waiting for
	# the global tool lock. Permission-filtered lists must not hide duplicates.
	rows = frappe.db.sql("select name from `tabCustomer` where customer_name = %s" + (" for update" if lock else ""), (customer_name,), as_dict=True)
	if rows:
		visible = []
		for row in rows:
			doc = frappe.get_doc("Customer", row.name)
			if frappe.has_permission("Customer", "read", doc=doc):
				visible.append({"customer_id": doc.name, "customer_name": doc.customer_name, "disabled": bool(doc.get("disabled"))})
		raise ProfileInputError("存在同名客户，未创建重复档案。请提供可访问的明确客户编号。", missing=["customer_id"], candidates=visible)
	return None


def _linked_documents(doctype, customer_id, lock):
	rows = frappe.db.sql("""select distinct p.name from `tabDynamic Link` l
		inner join `tab%s` p on p.name = l.parent
		where l.parenttype = %%s and l.parentfield = 'links'
		and l.link_doctype = 'Customer' and l.link_name = %%s""" % doctype + (" for update" if lock else ""), (doctype, customer_id), as_dict=True)
	return [frappe.get_doc(doctype, row.name, for_update=lock) for row in rows]


def _phone_key(value):
    return re.sub(r"[^0-9]", "", str(value or ""))


def _person_name(value):
    return _key(" ".join(str(value.get(k) or "").strip() for k in ("first_name", "middle_name", "last_name") if value.get(k)))


def _single_supplement(docs, target, identity):
    matches = [doc for doc in docs if identity(doc, target)]
    if not matches:
        return None
    if len(matches) != 1:
        raise ProfileInputError("已有多条相似联系人或地址，请先在原生档案中核对，未创建重复资料。")
    doc = matches[0]
    doc.check_permission("read")
    if doc.get("disabled"):
        raise ProfileInputError("匹配资料已停用，请先核对原生档案。")
    # Filling shared records would also change another customer's information.
    if any(r.link_doctype == "Customer" and r.link_name != target.get("_customer_id") for r in doc.get("links", [])):
        raise ProfileInputError("相似资料关联了多个客户，请在原生档案中确认本次修改范围。")
    return doc


def _contact_supplement(docs, target, customer_id):
    doc = _single_supplement(docs, dict(target, _customer_id=customer_id), lambda a,b: _person_name(a) == _person_name(b))
    if not doc:
        return None, {}
    updates = {}
    for key in ("company_name", "designation", "department"):
        value = target.get(key)
        if value and _key(doc.get(key)) != _key(value):
            if doc.get(key):
                raise ProfileInputError("已有同名联系人的非空资料与输入冲突，请在原生档案中确认。", fields=[key])
            updates[key] = value
    for table, field, flags, keyer in (("email_ids","email_id",("is_primary",),_key), ("phone_nos","phone",("is_primary_phone","is_primary_mobile_no"),_phone_key)):
        rows = [row.as_dict() if callable(getattr(row,"as_dict",None)) else dict(row) for row in doc.get(table, [])]
        changed = False
        for requested in target.get(table, []):
            found = next((row for row in rows if keyer(row.get(field)) == keyer(requested.get(field))), None)
            for flag in flags:
                if requested.get(flag) and any(row.get(flag) and keyer(row.get(field)) != keyer(requested.get(field)) for row in rows):
                    raise ProfileInputError("已有联系人的主邮箱或主电话与输入冲突，请在原生档案明确修改。", fields=[field])
            if found is None:
                rows.append(dict(requested)); changed=True
            else:
                for flag in flags:
                    if requested.get(flag) and not found.get(flag):
                        found[flag]=1; changed=True
        if changed:
            updates[table]=rows
    if updates:
        doc.check_permission("write")
    return doc, updates


def _address_supplement(docs, target, customer_id):
    # A change of business purpose needs review; do not silently clone the same
    # place under a different type and leave contradictory defaults behind.
    if target.get("address_type") == "货代收货":
        for existing in docs:
            if existing.get("address_type") != "货代收货" and all(_key(existing.get(k)) == _key(target.get(k)) for k in ADDRESS_IDENTITY if k != "address_type"):
                existing.check_permission("read")
                raise ProfileInputError("该收货地点已有其他地址类型的记录，请先在客户档案核对并改为货代收货，未创建重复地址。", fields=["address_type"])
    doc = _single_supplement(docs, dict(target,_customer_id=customer_id), lambda a,b: all(_key(a.get(k)) == _key(b.get(k)) for k in ADDRESS_IDENTITY))
    if not doc:
        return None, {}
    updates={}
    for key in ("email_id","phone","mobile_no","territory","address_title"):
        value=target.get(key)
        equal=(_phone_key if key in ("phone","mobile_no") else _key)
        if value and equal(doc.get(key)) != equal(value):
            if doc.get(key):
                raise ProfileInputError("同一地址的已有联系方式与输入冲突，请在原生地址中确认。",fields=[key])
            updates[key]=value
    if updates:
        doc.check_permission("write")
    return doc,updates


def _contact_matches(doc, target):
	# Different native name splitting must not turn one person into duplicates.
	full_name = lambda values: " ".join(str(values.get(key) or "").strip() for key in ("first_name", "middle_name", "last_name") if values.get(key))
	if _key(full_name(doc)) != _key(full_name(target)) or _key(doc.get("company_name")) != _key(target.get("company_name")):
		return False
	for row in target.get("email_ids", []):
		if not any(_key(existing.email_id) == _key(row.get("email_id")) and (not row.get("is_primary") or existing.get("is_primary")) for existing in doc.get("email_ids", [])):
			return False
	for row in target.get("phone_nos", []):
		if not any(_phone_key(existing.phone) == _phone_key(row.get("phone")) and all(not row.get(flag) or existing.get(flag) for flag in ("is_primary_phone", "is_primary_mobile_no")) for existing in doc.get("phone_nos", [])):
			return False
	return all(not target.get(field) or _key(doc.get(field)) == _key(target[field]) for field in ("designation", "department"))


def _address_matches(doc, target):
	return all(_key(doc.get(key)) == _key(target.get(key)) for key in ADDRESS_IDENTITY) and all(not target.get(key) or (_phone_key if key in ("phone", "mobile_no") else _key)(doc.get(key)) == (_phone_key if key in ("phone", "mobile_no") else _key)(target[key]) for key in ("email_id", "phone", "mobile_no", "territory", "address_title"))


def _find_match(docs, target, matcher):
	matches = [doc for doc in docs if matcher(doc, target)]
	if not matches:
		return None
	# Never clone a hidden exact match just because the user cannot read it.
	readable = [doc for doc in matches if frappe.has_permission(doc.doctype, "read", doc=doc)]
	if not readable:
		raise frappe.PermissionError("已有匹配资料不可访问。")
	active = [doc for doc in readable if not doc.get("disabled")]
	if not active:
		raise ProfileInputError("匹配的联系人或地址已停用，请先在原生档案核实，未创建重复资料。")
	return sorted(active, key=lambda doc: (not bool(doc.get("is_primary_contact") or doc.get("is_primary_address")), doc.name))[0]


def _contact_values(target, customer_id, primary):
	values = {key: target[key] for key in CONTACT_IDENTITY + ("designation", "department") if target.get(key)}
	values.update({"doctype": "Contact", "is_primary_contact": int(primary), "links": [{"link_doctype": "Customer", "link_name": customer_id}], "email_ids": [dict(row) for row in target.get("email_ids", [])], "phone_nos": [dict(row) for row in target.get("phone_nos", [])]})
	return values


def _address_values(target, customer_id, primary=False, shipping=False):
	return {"doctype": "Address", **_allowed("Address", target), "is_primary_address": int(primary), "is_shipping_address": int(shipping), "links": [{"link_doctype": "Customer", "link_name": customer_id}]}


def _permissions_new():
	for doctype in ("Customer", "Contact", "Address"):
		frappe.has_permission(doctype, "create", throw=True)
	# Native Customer creates Contact; subsequent writes complete person details
	# and address purpose without requiring Customer.write for a new customer.
	for doctype in ("Contact", "Address"):
		frappe.has_permission(doctype, "write", throw=True)


def _new_profile(profile, steps):
	customer_values, contact = profile["customer"], profile["contact"]
	shipping, billing = profile["shipping_address"], profile.get("billing_address")
	billing_mode = profile["address_policy"]["billing_mode"]
	shared_address = billing_mode == "same_as_shipping"
	values = {"doctype": "Customer", **customer_values}
	for key in ("email_id", "mobile_no"):
		values[key] = contact.get(key, "")
	# Native Individual creation splits customer_name. Passing the complete
	# contact name as first_name would overwrite only the first part and repeat
	# the middle/last names. Preserve that native split until completing Contact.
	if customer_values.get("customer_type") != "Individual":
		for key in ("first_name", "last_name"):
			values[key] = contact.get(key, "")
	elif not (values.get("email_id") or values.get("mobile_no")):
		# Trigger native Contact creation even when the customer explicitly has
		# not supplied their own phone. Do not borrow the forwarder's phone.
		values["first_name"] = customer_values["customer_name"].split()[0]
	# Passing an inline street to Customer would unconditionally create a
	# Billing address. Insert the actual reviewed Address types separately.
	steps.begin("save_customer", "通过原生流程创建客户和主联系人，地址按已审核用途分别保存。")
	customer = frappe.get_doc(values).insert()
	customer.reload()
	if not customer.customer_primary_contact:
		frappe.throw("原生客户创建未生成主联系人。")
	steps.done("客户和主联系人已创建，未把收货地址自动当作开票地址。", "saved")
	steps.begin("save_contact", "补全联系人姓名、邮箱、电话及主号码标记。")
	person = frappe.get_doc("Contact", customer.customer_primary_contact)
	person.check_permission("write")
	person_values = _contact_values(contact, customer.name, True)
	if _person_name(person) == _person_name(contact):
		for key in ("first_name", "middle_name", "last_name"):
			person_values[key] = person.get(key) or ""
	else:
		# This is a newly-created Contact, not an existing customer's identity.
		# Clear native leftover name parts when a different contact was requested.
		for key in ("first_name", "middle_name", "last_name"):
			person_values[key] = contact.get(key) or ""
	for key, value in person_values.items():
		if key != "doctype":
			person.set(key, value)
	person.save()
	steps.done("联系人详细资料已写入，姓名采用完整姓名核对并保留正确的原生拆分。", "saved")
	steps.begin("save_shipping_address", "以真实的收货用途创建地址，并设置首选送货地址。")
	shipping_doc = frappe.get_doc(_address_values(shipping, customer.name, shared_address, True)).insert()
	steps.done("收货地址已创建并设为首选送货地址；" + ("已明确确认兼作开票地址。" if shared_address else "未兼作开票地址。"), "saved")
	steps.begin("save_billing_address", "按审核结果保存独立开票地址、明确共用，或保留未提供状态。")
	if billing:
		billing_doc = frappe.get_doc(_address_values(billing, customer.name, True, False)).insert()
		steps.done("独立开票地址已创建并设为首选开票地址。", "saved")
	else:
		billing_doc = None
		steps.done("已明确确认直收地址兼作开票地址，无需重复创建。" if shared_address else "客户尚未提供开票地址，首选开票地址和客户首选地址保持空白。", "not_required")
	primary = billing_doc or (shipping_doc if shared_address else None)
	if primary:
		from frappe.contacts.doctype.address.address import get_address_display

		# Like native Customer.create_primary_address, initialize only the
		# just-created customer's links; no extra Customer.write is required.
		customer.db_set({"customer_primary_address": primary.name, "primary_address": get_address_display(primary.name)})
	profile["_expected_customer_primary_address"] = primary.name if primary else None
	profile["_expected_address_flags"] = {shipping_doc.name: {"is_primary_address": int(shared_address), "is_shipping_address": 1}}
	if billing_doc:
		profile["_expected_address_flags"][billing_doc.name] = {"is_primary_address": 1, "is_shipping_address": 0}
	return customer, person, shipping_doc, billing_doc, [{"doctype": "Customer", "name": customer.name, "action": "created"}, {"doctype": "Contact", "name": person.name, "action": "created"}, {"doctype": "Address", "name": shipping_doc.name, "action": "created"}] + ([{"doctype": "Address", "name": billing_doc.name, "action": "created"}] if billing_doc else [])


def _existing_plan(customer, profile, lock):
	updates, conflicts = {}, []
	for key, value in profile["customer"].items():
		if not value or key not in CUSTOMER_FIELDS:
			continue
		old = customer.get(key)
		if old and _key(old) != _key(value):
			conflicts.append({"field": key, "existing": old, "requested": value})
		elif not old:
			updates[key] = value
	if conflicts:
		raise ProfileInputError("以下已有客户信息与本次输入冲突，未覆盖。需要变更请在客户档案明确编辑。", conflicts=conflicts)
	contacts = _linked_documents("Contact", customer.name, lock)
	addresses = _linked_documents("Address", customer.name, lock)
	profile["_expected_address_flags"] = {doc.name: {key: int(bool(doc.get(key))) for key in ADDRESS_FLAGS} for doc in addresses}
	profile["existing_address_preferences"] = {"customer_primary_address": customer.get("customer_primary_address"), "has_preferred_billing": any(doc.get("is_primary_address") for doc in addresses), "has_preferred_shipping": any(doc.get("is_shipping_address") for doc in addresses)}
	person = _find_match(contacts, profile["contact"], _contact_matches) if profile.get("contact") else None
	shipping = _find_match(addresses, profile["shipping_address"], _address_matches) if profile.get("shipping_address") else None
	billing = _find_match(addresses, profile["billing_address"], _address_matches) if profile.get("billing_address") else None
	contact_updates, shipping_updates, billing_updates = {}, {}, {}
	if not person and profile.get("contact"):
		person, contact_updates = _contact_supplement(contacts, profile["contact"], customer.name)
	if not shipping and profile.get("shipping_address"):
		shipping, shipping_updates = _address_supplement(addresses, profile["shipping_address"], customer.name)
	if not billing and profile.get("billing_address"):
		billing, billing_updates = _address_supplement(addresses, profile["billing_address"], customer.name)
	if shipping and profile.get("address_policy", {}).get("shipping_type") == "货代收货" and (shipping.get("is_primary_address") or customer.get("customer_primary_address") == shipping.name):
		raise ProfileInputError("已有货代收货地址被选作客户首选或首选开票地址，请先在客户档案核对，未自动修改已有资料。", fields=["customer_primary_address", "is_primary_address"])
	refresh_customer = bool(contact_updates and person and person.name == customer.get("customer_primary_contact"))
	create_person = bool(profile.get("contact") and not person)
	create_shipping = bool(profile.get("shipping_address") and not shipping)
	create_billing = bool(profile.get("billing_address") and not billing)
	set_contact = bool(profile.get("contact") and not customer.get("customer_primary_contact"))
	shared_address = profile["address_policy"]["billing_mode"] == "same_as_shipping"
	primary_target = billing or (shipping if shared_address else None)
	primary_requested = bool(profile.get("billing_address") or (profile.get("shipping_address") and shared_address))
	existing_primary = [doc for doc in addresses if doc.get("is_primary_address")]
	set_address = bool(primary_requested and not customer.get("customer_primary_address") and (not existing_primary or (primary_target and any(doc.name == primary_target.name for doc in existing_primary))))
	set_shipping = bool(profile.get("shipping_address") and not any(doc.get("is_shipping_address") for doc in addresses))
	if set_shipping and shipping:
		shipping_updates["is_shipping_address"] = 1
		shipping.check_permission("write")
	if set_address and primary_target and not primary_target.get("is_primary_address"):
		(shipping_updates if shared_address else billing_updates)["is_primary_address"] = 1
		primary_target.check_permission("write")
	if updates or set_contact or set_address or refresh_customer:
		customer.check_permission("write")
	if create_person:
		frappe.has_permission("Contact", "create", throw=True)
	if create_shipping or create_billing:
		frappe.has_permission("Address", "create", throw=True)
	# Native Customer.save reaffirms primary flags, so preflight the write checks
	# for whichever linked records its controller will touch.
	if updates or set_contact or set_address or refresh_customer:
		for doctype, name in (("Contact", customer.get("customer_primary_contact")), ("Address", customer.get("customer_primary_address"))):
			if name:
				frappe.get_doc(doctype, name).check_permission("write")
	return dict(contact_updates=contact_updates, shipping_updates=shipping_updates, billing_updates=billing_updates, refresh_customer=refresh_customer, updates=updates, contact=person, shipping=shipping, billing=billing, create_person=create_person, create_shipping=create_shipping, create_billing=create_billing, set_contact=set_contact, set_address=set_address, set_shipping=set_shipping)


def _complete_existing(customer, profile, plan, steps):
	changes = []
	person, shipping, billing = plan["contact"], plan["shipping"], plan["billing"]
	def complete(step, doc, target, patch, create, values):
		steps.begin(step, "复用匹配记录、补充空白资料，或按预检计划创建缺少的关联记录。")
		if not target:
			steps.done("本次未提供此项资料，无需修改。", "not_required")
		elif patch:
			doc.update(patch)
			doc.save()
			changes.append({"doctype": doc.doctype, "name": doc.name, "action": "filled_empty_fields", "fields": sorted(patch)})
			steps.done("仅补充空白资料或缺少的首选标记，保留原有身份和已选首选资料。", "saved")
		elif create:
			doc = frappe.get_doc(values()).insert()
			changes.append({"doctype": doc.doctype, "name": doc.name, "action": "created"})
			steps.done("原客户缺少本次所需的关联记录，已创建并关联；保留已有首选资料。", "saved")
		else:
			steps.done("已有资料与本次输入匹配，直接复用，未重复创建或修改。", "reused")
		if doc and doc.doctype == "Address":
			profile.setdefault("_expected_address_flags", {})[doc.name] = {key: int(bool(doc.get(key))) for key in ADDRESS_FLAGS}
		return doc
	person = complete("save_contact", person, profile.get("contact"), plan["contact_updates"], plan["create_person"], lambda: _contact_values(profile["contact"], customer.name, plan["set_contact"]))
	billing = complete("save_billing_address", billing, profile.get("billing_address"), plan["billing_updates"], plan["create_billing"], lambda: _address_values(profile["billing_address"], customer.name, plan["set_address"], False))
	# Supplementary addresses never displace an existing shipping preference.
	shipping = complete("save_shipping_address", shipping, profile.get("shipping_address"), plan["shipping_updates"], plan["create_shipping"], lambda: _address_values(profile["shipping_address"], customer.name, plan["set_address"] and profile["address_policy"]["billing_mode"] == "same_as_shipping", plan["set_shipping"]))
	steps.begin("save_customer", "补充客户空白字段与缺少的首选关联，保留已有非空资料。")
	updates = dict(plan["updates"])
	if plan["set_contact"]:
		updates["customer_primary_contact"] = person.name
	if plan["set_address"]:
		updates["customer_primary_address"] = (billing or shipping).name
	profile["_expected_customer_primary_address"] = updates.get("customer_primary_address", customer.get("customer_primary_address"))
	if updates or plan["refresh_customer"]:
		customer.update(updates)
		customer.save()
		changes.append({"doctype": "Customer", "name": customer.name, "action": "filled_empty_fields", "fields": sorted(updates)})
		steps.done("客户空白资料或缺少的首选关联已补齐，已有非空资料未覆盖。", "saved")
	else:
		steps.done("客户基础资料与关联均完整匹配，未修改客户档案。", "reused")
	return customer, person, shipping, billing, changes


def _verify(customer, person, shipping, billing, profile, original):
	customer = frappe.get_doc("Customer", customer.name)
	customer.check_permission("read")
	if profile.get("territory_plan", {}).get("source") == "customer_country_not_provided" and customer.get("territory"):
		raise ProfileVerificationError("客户", ["territory"], "客户国家暂未提供，销售地区应保持空白")
	for field, value in profile["customer"].items():
		if value and _key(customer.get(field)) != _key(value):
			raise ProfileVerificationError("客户", [field], FIELD_LABELS.get(field, field) + "与本次资料不一致")
	for field in ("customer_primary_contact", "customer_primary_address", "mobile_no", "email_id"):
		if original and original.get(field) and _key(customer.get(field)) != _key(original[field]):
			raise ProfileVerificationError("客户", [field], "已有" + FIELD_LABELS.get(field, field) + "意外改变")
	if _key(customer.get("customer_primary_address")) != _key(profile.get("_expected_customer_primary_address")):
		raise ProfileVerificationError("客户", ["customer_primary_address"], "客户首选地址未按已确认的开票用途保存")
	for name, expected in profile.get("_expected_address_flags", {}).items():
		address = frappe.get_doc("Address", name)
		changed = [key for key, value in expected.items() if int(bool(address.get(key))) != value]
		if changed:
			raise ProfileVerificationError("地址", changed, "首选开票或首选送货标记与已审核安排不一致")
	result_docs = []
	for label, doc, target, matcher in (("联系人", person, profile.get("contact"), _contact_matches), ("收货地址", shipping, profile.get("shipping_address"), _address_matches), ("账单地址", billing, profile.get("billing_address"), _address_matches)):
		if not target:
			result_docs.append(None)
			continue
		if not doc:
			raise ProfileVerificationError(label, [], "建档所需关联记录缺失")
		verified = frappe.get_doc(doc.doctype, doc.name)
		verified.check_permission("read")
		if not _linked(verified, "Customer", customer.name):
			raise ProfileVerificationError(label, ["links"], "未正确关联本次客户")
		if not matcher(verified, target):
			if doc.doctype == "Contact":
				fields = [field for field in ("company_name", "designation", "department") if (field == "company_name" or target.get(field)) and _key(verified.get(field)) != _key(target.get(field))]
				if _person_name(verified) != _person_name(target):
					fields.insert(0, "full_name")
				identity = {field: verified.get(field) for field in CONTACT_IDENTITY + ("designation", "department")}
				for table in ("email_ids", "phone_nos"):
					requested = {**identity, "email_ids": [], "phone_nos": [], table: target.get(table, [])}
					if not _contact_matches(verified, requested):
						fields.append(table)
			else:
				fields = [field for field in ADDRESS_IDENTITY if _key(verified.get(field)) != _key(target.get(field))]
				fields += [field for field in ("email_id", "phone", "mobile_no", "territory", "address_title") if target.get(field) and (_phone_key if field in ("phone", "mobile_no") else _key)(verified.get(field)) != (_phone_key if field in ("phone", "mobile_no") else _key)(target[field])]
			raise ProfileVerificationError(label, fields, "、".join(FIELD_LABELS.get(field, field) for field in fields) + "与本次资料不一致")
		result_docs.append(verified)
	return customer, *result_docs


def _review_summary(profile, customer=None, shipping=None, billing=None):
	policy = profile["address_policy"]
	shipping_values = profile.get("shipping_address") or {}
	billing_values = profile.get("billing_address") or {}
	contact = profile.get("contact") or {}
	mode = policy["billing_mode"]
	existing_billing = bool(profile.get("existing_address_preferences", {}).get("customer_primary_address") or profile.get("existing_address_preferences", {}).get("has_preferred_billing"))
	billing_label = {"same_as_shipping": "已明确确认与直收地址相同", "separate": "独立开票地址", "not_provided": "本次未提供，保留客户已有开票设置" if existing_billing else "未提供，首选开票地址及客户首选地址留空"}[mode]
	return {
		"customer_name": profile["customer"].get("customer_name"),
		"account_manager": profile["customer"].get("account_manager"),
		"customer_location": profile.get("territory_location", {}),
		"territory": profile["customer"].get("territory"),
		"territory_label": profile["customer"].get("territory") or "暂未提供，销售地区留空（待补充）",
		"currency": profile["customer"].get("default_currency"),
		"contact": {"name": " ".join(contact.get(key, "") for key in ("first_name", "middle_name", "last_name") if contact.get(key)), "phone": contact.get("mobile_no") or contact.get("phone_no") or "未提供", "email": contact.get("email_id") or "未提供"},
		"shipping": {"type": policy["shipping_type"], "label": "货代代收（每单核对地址）" if policy["shipping_type"] == "货代收货" else "客户直收", "recipient": shipping_values.get("address_title"), "phone": shipping_values.get("phone"), "address": {key: shipping_values.get(key) for key in ADDRESS_IDENTITY if key != "address_type"}, **({key: bool(shipping.get(key)) for key in ADDRESS_FLAGS} if shipping else {})},
		"billing": {"mode": mode, "label": billing_label, "address": billing_values if mode == "separate" else (shipping_values if mode == "same_as_shipping" else None), "preserves_existing": existing_billing},
		"customer_primary_address": customer.get("customer_primary_address") if customer else None,
		"review_instruction": "请整体核对客户所在地、联系人、收货用途及收货电话、开票地址安排后批准一次。",
	}


def _result(customer, person, shipping, billing, profile, changes, created):
	billing_result = billing or (shipping if profile["address_policy"]["billing_mode"] == "same_as_shipping" else None)
	return {"status": "created" if created else ("updated" if changes else "reused"), "customer_id": customer.name,
		"customer": {"name": customer.name, **{field: customer.get(field) for field in CUSTOMER_FIELDS}, "primary_contact": customer.customer_primary_contact, "primary_address": customer.customer_primary_address},
		"contact": ({"name": person.name, "full_name": person.full_name, "email_id": person.email_id, "mobile_no": person.mobile_no, "phone": person.phone} if person else None),
		"shipping_address": ({"name": shipping.name, **{key: shipping.get(key) for key in ADDRESS_IDENTITY + ADDRESS_FLAGS + ("address_title", "phone")}} if shipping else None),
		"billing_address": ({"name": billing_result.name, **{key: billing_result.get(key) for key in ADDRESS_IDENTITY + ADDRESS_FLAGS}} if billing_result else None),
		"address_policy": profile["address_policy"], "review_summary": _review_summary(profile, customer, shipping, billing),
		"changes": changes, "warnings": profile.get("warnings", []), "defaults_used": profile.get("defaults_used", {}), "verified": True,
		"message": "客户及关联资料已保存并回读核验。" if changes else "已有资料完全匹配，无需重复创建。"}


def create_or_reuse_customer_profile(
	customer_name: str = "", customer_type: str = "", address_line1: str = "", city: str = "", country: str = "",
	mobile_no: str = "", contact_name: str = "", email_id: str = "", address_line2: str = "", state: str = "",
	county: str = "", pincode: str = "", customer_group: str = "", default_currency: str = "",
	confirm_existing_customer: bool = False, customer_id: str = "", dry_run: bool = False,
	phone_no: str = "", territory: Annotated[str, "自动填写，普通客户建档省略此参数。后端从客户实际国家、省州、城市匹配并自动创建地区；货代只知客户国家时先用国家地区，不要要求用户选择或单独批准地区；仅用户明确指定已有地区时填写。"] = "", default_price_list: str = "",
	payment_terms: str = "", tax_id: str = "", tax_category: str = "", language: str = "", website: str = "",
	customer_details: str = "", industry: str = "", market_segment: str = "", contact_designation: str = "", contact_department: str = "",
	postal_code_not_applicable: bool = False, billing_address: dict | None = None,
	account_manager: str = "",
	address_type: str = "", billing_address_mode: str = "",
	customer_country: str = "", customer_state: str = "", customer_city: str = "",
	shipping_phone: str = "", shipping_contact_name: str = "", contact_phone_not_provided: bool = False,
	customer_country_not_provided: Annotated[bool, "仅货代收货：客服明确客户所在国家未知或暂不提供时设为 true，不再追问国家；新客户销售地区留空，已有地区保留。收货仓的国家和完整地址仍需提供；客户电话若提供必须带国际区号，不从电话推断客户国家。"] = False,
) -> dict:
	"""Create a full native customer profile or safely supplement an explicit ID.

	Customer name/type, currency, named contact, explicit shipping purpose and
	billing arrangement, and a complete delivery address form the new-profile
	basics. address_type is Shipping (direct) or 货代收货 (forwarder).
	billing_address_mode is same_as_shipping (explicit direct-address consent),
	separate, or not_provided; forwarders cannot double as billing addresses.
	Forwarders require their own shipping_phone. Supply the real customer_country,
	or set customer_country_not_provided when explicitly unknown; new customer
	territory remains blank and existing territory is preserved.
	if the customer's phone is unknown, explicitly set contact_phone_not_provided.
	Optional invoicing and commercial terms are preserved. Missing facts are never
	invented. Existing customer IDs are mandatory for supplementary operations;
	confirm_existing_customer remains accepted for older calls but cannot select
	an ambiguous customer. The Flow wrapper owns one confirmation and the commit.
	account_manager is the responsible enabled System User ID, defaulting to the
	current user only for new customers. Existing responsibility is preserved;
	unassigned existing customers require an explicit account_manager.
	Omit territory for ordinary onboarding: the backend derives the city from the
	customer's location and creates missing territory records with the customer.
	For a forwarder, never derive customer territory from the delivery warehouse.
	Never ask users to select existing territories or separately approve a city.
	Customer group is also automatic by customer type unless explicitly supplied.
	"""
	values = dict(locals())
	steps = ProfileSteps()
	steps.begin("validate_arguments", "检查预检开关、客户编号和名称的数据类型。")
	if type(dry_run) is not bool or type(confirm_existing_customer) is not bool:
		message = "dry_run 和 confirm_existing_customer 必须为 true 或 false。"
		return steps.result({"status": "needs_input", "message": message, "failed_step": steps.fail(message), "next_action": "correct_tool_arguments"})
	if not isinstance(customer_id, str) or not isinstance(customer_name, str):
		message = "客户编号和名称必须是文本。"
		return steps.result({"status": "needs_input", "message": message, "failed_step": steps.fail(message), "next_action": "correct_tool_arguments"})
	steps.done("调用参数类型正确，可以核对客户档案。")
	customer_id, customer_name = customer_id.strip(), customer_name.strip()
	values.update(customer_id=customer_id, customer_name=customer_name)
	savepoint = "customer_profile_" + uuid4().hex
	frappe.db.savepoint(savepoint)
	touched = []
	try:
		steps.begin("resolve_customer", "核对指定客户的访问权限、启用状态及同名重复档案。")
		if not dry_run:
			# Low-volume profile creation uses one stable existing row, independent
			# of customer group/name spelling. Lock lasts until the Flow commit.
			frappe.db.sql("select name from `tabDocType` where name = 'Customer' for update")
		customer = _resolve_customer(customer_id, customer_name, lock=not dry_run)
		if customer:
			steps.done("已找到并核对明确指定的客户，将补充其档案。")
		elif customer_name:
			steps.done("未发现同名客户，将按新客户建档。")
		else:
			steps.done("尚未提供客户名称，需补齐后才能核对同名客户。", "not_executed")
		original = customer.as_dict() if customer else None
		steps.begin("validate_profile", "校验必填资料、电话号码及地址格式，并应用客户分组和负责人默认值。")
		profile = normalize_profile({key: value for key, value in values.items() if key in INPUT_FIELDS}, existing=original)
		profile["shipping_address"] = profile.pop("shipping")
		profile["billing_address"] = profile.pop("billing")
		contact = profile.get("contact", {})
		contact["email_id"] = next((row["email_id"] for row in contact.get("email_ids", []) if row.get("is_primary")), "")
		contact["mobile_no"] = next((row["phone"] for row in contact.get("phone_nos", []) if row.get("is_primary_mobile_no")), "")
		contact["phone_no"] = next((row["phone"] for row in contact.get("phone_nos", []) if row.get("is_primary_phone")), "")
		automatic_fields = {}
		location = profile.get("territory_location") or profile["shipping_address"]
		if not values.get("territory") and not (original or {}).get("territory"):
			unknown_country = bool(location.get("country_not_provided"))
			automatic_fields["territory"] = {
				"mode": "leave_blank_customer_country_unknown" if unknown_country else "match_or_create_customer_location", "requires_user_selection": False,
				"requires_separate_confirmation": False,
				"country": location.get("country"), "state": location.get("state"), "city": location.get("city"),
				"message": "客户国家暂未提供，销售地区留空，待补充；不使用货代仓所在地。" if unknown_country else "销售地区由客户实际所在地自动匹配；货代仓所在地不能代替客户所在地，无需询问地区选择。",
			}
		profile["automatic_fields"] = automatic_fields
		if profile.get("missing") or profile.get("errors"):
			missing = profile.get("missing", [])
			reason = "缺少：" + "、".join(FIELD_LABELS.get(field, field) for field in missing) + "。" if missing else ""
			if profile.get("errors"):
				reason += "资料格式或内容未通过校验，详见 errors 的具体原因。"
			return steps.result({"status": "needs_input", "message": "仅需补齐 missing 中的资料，或修正 errors 指出的资料问题；销售地区自动处理，不要额外询问地区选择。本次未写入。", "missing": missing, "errors": profile.get("errors", []), "warnings": profile.get("warnings", []), "defaults_used": profile.get("defaults_used", {}), "automatic_fields": automatic_fields, "failed_step": steps.fail(reason), "next_action": "supply_missing_or_correct_invalid_fields"})
		steps.done("客户、联系人和地址必填资料及格式通过校验；已按规则应用默认值。")
		steps.begin("plan_territory", "依据客户实际所在地匹配销售地区，并规划缺少的地区记录。")
		territory_plan = plan_customer_territory(profile["customer"], location, existing=original, lock=not dry_run)
		profile["customer"]["territory"] = territory_plan["territory"]
		profile["territory_plan"] = territory_plan
		profile.setdefault("defaults_used", {})["territory"] = {"value": territory_plan["territory"], "source": territory_plan["source"]}
		unknown_territory = territory_plan.get("source") == "customer_country_not_provided"
		steps.done("客户国家暂未提供，销售地区保持空白，待后续补充。" if unknown_territory else ("已规划自动创建缺少的国家／城市地区，尚未写入，无需另选地区。" if territory_plan.get("will_create") else "已匹配可用的销售地区，复用已有地区，无需另选地区。"))
		steps.begin("check_permissions", "检查客户、联系人和地址所需权限；已有客户仅允许补空，不覆盖冲突资料。")
		if customer:
			plan = _existing_plan(customer, profile, lock=not dry_run)
		else:
			_permissions_new()
			plan = None
		steps.done("本次操作所需权限检查通过，未发现需阻止的已有资料冲突。")
		if dry_run:
			return steps.result({"status": "preview", "customer_id": customer.name if customer else None, "profile": {key: value for key, value in profile.items() if not key.startswith("_")}, "review_summary": _review_summary(profile, customer), "will_create_customer": customer is None, "next_action": "create_or_reuse_customer_profile", "message": "预检通过，尚未写入；请整体审核客户所在地、收货用途和开票地址安排后批准一次。销售地区自动处理，无需单独确认。"})
		steps.begin("save_territory", "复用已匹配地区，或在同一事务中创建缺少的国家／城市地区。")
		territory_changes = apply_customer_territory(territory_plan)
		steps.done("客户国家暂未提供，未创建地区记录，客户销售地区留空。" if unknown_territory else (f"已创建 {len(territory_changes)} 条缺少的销售地区记录，等待客户整体核验。" if territory_changes else "已复用已有销售地区，未重复创建。"), "not_required" if unknown_territory else ("saved" if territory_changes else "reused"))
		if customer:
			customer, person, shipping, billing, changes = _complete_existing(customer, profile, plan, steps)
		else:
			customer, person, shipping, billing, changes = _new_profile(profile, steps)
		changes = territory_changes + changes
		touched = [(doc.doctype, doc.name) for doc in (customer, person, shipping, billing) if doc] + [("Territory", row["name"]) for row in territory_changes]
		steps.begin("verify_profile", "重新读取客户、联系人、收货／账单地址，逐项核对资料和客户关联。")
		customer, person, shipping, billing = _verify(customer, person, shipping, billing, profile, original)
		steps.done("客户及关联记录回读一致，原有首选资料未被意外改变。")
		return steps.result({**_result(customer, person, shipping, billing, profile, changes, not bool(original)), "next_action": "open_customer_profile"})
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		for doctype, name in touched:
			frappe.clear_document_cache(doctype, name)
		if isinstance(exc, ProfileInputError):
			return steps.result({"status": "needs_input", "message": str(exc), "rolled_back": True, **exc.details, "failed_step": steps.fail(str(exc), rolled_back=True), "next_action": "review_reported_conflict_or_missing_fields"})
		if isinstance(exc, frappe.PermissionError):
			message = "当前用户缺少本次所需的客户、联系人、地址或销售区域权限，本次未保留任何修改。"
			next_action = "ask_administrator_to_check_permissions"
		elif isinstance(exc, ProfileVerificationError):
			message = str(exc)
			next_action = "contact_administrator_do_not_repeat_creation"
		elif isinstance(exc, frappe.ValidationError):
			message = str(exc)
			next_action = "review_validation_error_before_retry"
		else:
			message = "系统在“" + steps.current["label"] + "”发生异常，本次客户、联系人、地址和销售区域修改已完整回滚，请联系管理员，不要重复创建。"
			next_action = "contact_administrator_do_not_repeat_creation"
		error_id = None
		if isinstance(exc, ProfileVerificationError) or not isinstance(exc, (frappe.PermissionError, frappe.ValidationError)):
			try:
				# No customer argument bundle or hidden duplicate records in the log.
				logged = frappe.log_error(title="Flow 客户建档失败: " + steps.current["step"], message=frappe.get_traceback())
				error_id = getattr(logged, "name", None)
			except Exception:
				pass  # Logging failure must never defeat the business rollback.
		result = {"status": "error", "message": message, "error_type": type(exc).__name__, "rolled_back": True, "failed_step": steps.fail(message, rolled_back=True), "next_action": next_action}
		if isinstance(exc, ProfileVerificationError):
			result["verification_error"] = exc.details
		if error_id:
			result["error_id"] = error_id
		return steps.result(result)


_create_or_reuse_customer_profile = create_or_reuse_customer_profile


@wraps(_create_or_reuse_customer_profile)
def preview_customer_profile(*args, **kwargs):
	"""Read-only customer profile preflight; intentionally requires no confirmation."""
	kwargs["dry_run"] = True
	return _create_or_reuse_customer_profile(*args, **kwargs)


from flow.lib.tool import tool
from flow.integrations.erpnext.customer_profile_review import customer_confirmation_prompt


create_or_reuse_customer_profile = tool(
	_create_or_reuse_customer_profile,
	name="create_or_reuse_customer_profile",
	requires_confirmation=True,
	confirm_prompt=customer_confirmation_prompt,
)
