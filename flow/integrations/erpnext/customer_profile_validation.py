"""Read-only validation for the customer onboarding Flow tool."""

from urllib.parse import urlsplit

import frappe
import phonenumbers
from frappe.utils import validate_email_address


CUSTOMER_FIELDS = {
	"customer_name", "customer_type", "customer_group", "territory", "default_currency",
	"default_price_list", "payment_terms", "tax_id", "tax_category", "website", "language",
	"customer_details", "industry", "market_segment", "account_manager",
}
ADDRESS_FIELDS = {"address_line1", "address_line2", "city", "state", "county", "country", "pincode"}
INPUT_FIELDS = CUSTOMER_FIELDS | ADDRESS_FIELDS | {
	"mobile_no", "phone_no", "contact_name", "email_id", "contact_designation",
	"contact_department", "postal_code_not_applicable", "billing_address",
	"address_type", "billing_address_mode", "customer_country", "customer_state", "customer_city",
	"shipping_phone", "shipping_contact_name", "contact_phone_not_provided", "customer_country_not_provided",
}
BILLING_FIELDS = ADDRESS_FIELDS | {"postal_code_not_applicable", "email_id", "phone", "mobile_no"}
COUNTRY_ALIASES = {
	"us": "US", "usa": "US", "美国": "US", "united states of america": "US",
	"uk": "GB", "gb": "GB", "英国": "GB", "中国": "CN", "中国大陆": "CN",
	"中华人民共和国": "CN", "香港": "HK", "中国香港": "HK", "澳门": "MO", "中国澳门": "MO",
	"台湾": "TW", "中国台湾": "TW", "加拿大": "CA", "澳大利亚": "AU", "新西兰": "NZ",
	"德国": "DE", "法国": "FR", "日本": "JP", "韩国": "KR", "新加坡": "SG",
	"马来西亚": "MY", "泰国": "TH", "越南": "VN", "菲律宾": "PH", "印度": "IN",
	"印度尼西亚": "ID", "印尼": "ID", "俄罗斯": "RU", "意大利": "IT", "西班牙": "ES",
	"墨西哥": "MX", "巴西": "BR", "阿联酋": "AE",
}
LINKS = {
	"customer_group": "Customer Group", "territory": "Territory", "default_currency": "Currency",
	"default_price_list": "Price List", "payment_terms": "Payment Terms Template",
	"tax_category": "Tax Category", "language": "Language",
	"industry": "Industry Type", "market_segment": "Market Segment",
}
STATE_REQUIRED = {"US", "CA", "AU", "CN", "IN", "BR", "MX"}
SHIPPING_TYPES = {
	"shipping": "Shipping", "直收": "Shipping", "直发": "Shipping", "客户直收": "Shipping",
	"客户直接收货": "Shipping", "客户直收地址": "Shipping", "固定地址直收": "Shipping",
	"货代收货": "货代收货", "货代": "货代收货", "货代代收": "货代收货", "货代仓": "货代收货",
}
BILLING_MODES = {
	"same_as_shipping": "same_as_shipping", "同收货地址": "same_as_shipping", "与收货地址相同": "same_as_shipping",
	"separate": "separate", "独立": "separate", "独立发票地址": "separate", "单独提供": "separate",
	"not_provided": "not_provided", "未提供": "not_provided", "不知道": "not_provided", "暂未提供": "not_provided",
}


def _text(value, key, limit=140):
	if value is None:
		return ""
	if not isinstance(value, str):
		frappe.throw(f"{key} 必须是文本。")
	if any((ord(char) < 32 and not (key == "customer_details" and char in "\n\t")) or 127 <= ord(char) <= 159 for char in value):
		frappe.throw(f"{key} 不能包含控制字符。")
	value = value.strip()
	if len(value) > limit:
		frappe.throw(f"{key} 不能超过 {limit} 个字符。")
	return value


def _flag(values, key):
	value = values.get(key, False)
	if not isinstance(value, bool):
		frappe.throw(f"{key} 必须为 true 或 false。")
	return value


def _account_manager(requested, existing, defaults_used, missing):
	if not requested and existing is not None:
		manager = existing.get("account_manager")
		if manager:
			defaults_used["account_manager"] = {"value": manager, "source": "existing_customer"}
			return manager
		missing.append("account_manager")
		return ""
	manager = requested or frappe.session.user
	if not manager or manager == "Guest" or not frappe.db.exists("User", manager):
		frappe.throw("请指定有效的系统用户作为客户负责人（客户经理）。")
	user = frappe.get_doc("User", manager)
	if manager != frappe.session.user:
		user.check_permission("select")
	if not user.get("enabled") or user.get("user_type") != "System User":
		frappe.throw("客户负责人必须是已启用的系统用户，不能是停用账号或网站客户账号。")
	if not requested:
		defaults_used["account_manager"] = {"value": user.name, "source": "current_user"}
	return user.name


def _link(doctype, name):
	if not frappe.db.exists(doctype, name):
		frappe.throw(f"{doctype} 中不存在 {name}，请先选择有效的基础资料。")
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	if doc.get("disabled"):
		frappe.throw(f"{doctype} {name} 已停用。")
	if doctype == "Customer Group" and doc.get("is_group"):
		frappe.throw("客户分组必须选择末级分组，不能选择汇总组。")
	if doctype in ("Currency", "Price List") and not doc.get("enabled"):
		frappe.throw(f"{doctype} {name} 未启用。")
	if doctype == "Price List" and not doc.get("selling"):
		frappe.throw("默认价目表必须为销售价目表。")
	return doc


def _country(value):
	if not value:
		return "", None
	name = COUNTRY_ALIASES.get(value.casefold(), value)
	if not frappe.db.exists("Country", name):
		matches = frappe.get_list("Country", filters={"code": name.lower()}, fields=["name"], limit_page_length=2)
		if len(matches) != 1:
			frappe.throw(f"无法唯一识别国家 {value}，请提供系统中的国家名称或两位国家代码。")
		name = matches[0].get("name")
	doc = _link("Country", name)
	return doc.name, (doc.get("code") or "").upper() or None


def _default(key, existing, defaults_used, warnings):
	value = existing.get(key) if existing else None
	candidates = [("existing_customer", value)] if value else []
	for getter_name in ("get_user_default", "get_global_default"):
		getter = getattr(frappe.defaults, getter_name)
		for default_key in ((key,) if key == "default_currency" else (key, LINKS[key])):
			value = getter(default_key)
			if isinstance(value, str) and value.strip():
				candidates.append((getter_name.removeprefix("get_"), value.strip()))
	for source, value in candidates:
		try:
			doc = _link(LINKS[key], value)
		except (frappe.ValidationError, frappe.PermissionError):
			warnings.append(f"{key} 的已有默认值不可用，请确认此项。")
			continue
		defaults_used[key] = {"value": doc.name, "source": source}
		return doc.name
	return ""


def _email(value, key):
	if not value:
		return ""
	validated = validate_email_address(value, throw=True)
	if not validated or "," in validated or ";" in value:
		frappe.throw(f"{key} 只能填写一个有效邮箱。")
	return validated


def _phone(value, region, key, warnings, require_country_code=False):
	if not value:
		return None
	if not region and not value.startswith("+"):
		if require_country_code:
			frappe.throw(f"{key} 在客户实际所在国家未提供时，必须填写带国家区号（以 + 开头）的完整电话号码。")
		# Report the missing country together with other missing data first.
		return None
	try:
		number = phonenumbers.parse(value, region)
	except phonenumbers.NumberParseException:
		frappe.throw(f"{key} 无法识别，请提供带国家区号的完整电话号码。")
	if not phonenumbers.is_valid_number(number) or number.extension:
		frappe.throw(f"{key} 无效；请提供完整电话号码，分机请放在客户备注中。")
	# Keep significant leading zeros (for example Italian numbers) from E.164.
	# The supplied field determines mobile/landline; number databases cannot reliably
	# distinguish them in every country and must not silently change that choice.
	e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
	prefix = f"+{number.country_code}"
	return {"phone": f"{prefix} {e164[len(prefix):]}"}


def _address(values, address_type, missing, warnings, prefix=""):
	address = {key: values[key] for key in ADDRESS_FIELDS if values.get(key)}
	address["address_type"] = address_type
	for key in ("address_line1", "city", "country"):
		if not address.get(key):
			missing.append(prefix + key)
	country, region = _country(address.get("country", ""))
	if country:
		address["country"] = country
	if region in STATE_REQUIRED and not address.get("state"):
		missing.append(prefix + "state")
	if not address.get("pincode") and not _flag(values, "postal_code_not_applicable"):
		missing.append(prefix + "pincode")
	if _flag(values, "postal_code_not_applicable"):
		if address.get("pincode"):
			frappe.throw(f"{prefix}已填写邮编，不能同时声明邮编不适用。")
		warnings.append(f"{prefix or 'shipping.'}已明确声明邮编不适用，请确认承运人接受该地址。")
	email = _email(values.get("email_id", ""), prefix + "email_id")
	if email:
		address["email_id"] = email
	phone = _phone(values.get("phone") or values.get("phone_no") or values.get("mobile_no"), region, prefix + "phone", warnings)
	if phone:
		address["phone"] = phone["phone"]
	return address, region


def _address_policy(normalized, values, missing):
	requested_type = normalized.get("address_type", "")
	shipping_type = SHIPPING_TYPES.get(requested_type.casefold(), "")
	if requested_type and not shipping_type:
		frappe.throw("请明确收货地址为客户直收（Shipping）或货代收货；仓库不一定是货代，不能自动归类。")
	if not shipping_type:
		missing.append("address_type")
	requested_mode = normalized.get("billing_address_mode", "")
	billing_mode = BILLING_MODES.get(requested_mode.casefold(), "")
	if requested_mode and not billing_mode:
		frappe.throw("请明确发票地址为同收货地址、独立提供或未提供。")
	if values.get("billing_address") is not None:
		if billing_mode and billing_mode != "separate":
			frappe.throw("已提供独立发票地址，不能同时声明与收货地址共用或未提供。")
		billing_mode = "separate"
	if not billing_mode:
		missing.append("billing_address_mode")
	if billing_mode == "separate" and values.get("billing_address") is None:
		missing.append("billing_address")
	if shipping_type == "货代收货" and billing_mode == "same_as_shipping":
		frappe.throw("货代收货地址不能自动兼作客户发票地址。请单独提供客户真实发票地址，或明确标记未提供。")
	return {"shipping_type": shipping_type, "billing_mode": billing_mode}


def _customer_location(normalized, shipping, policy, missing, warnings):
	forwarder = policy["shipping_type"] == "货代收货"
	explicit = any(normalized.get("customer_" + key) for key in ("country", "state", "city"))
	if normalized["customer_country_not_provided"]:
		if not forwarder:
			frappe.throw("只有明确货代收货时，才可标记客户实际所在国家未提供。")
		if explicit:
			frappe.throw("已提供客户国家、省州或城市，不能同时标记客户实际所在国家未提供。")
		warnings.append("客户实际所在国家未知、待补充；不会根据货代仓地址或电话号码推断客户国家或销售区域，已有销售区域保留。")
		return {"country": "", "state": "", "city": "", "allow_country_only": True,
			"country_not_provided": True}, None
	if forwarder or explicit:
		location = {key: normalized.get("customer_" + key, "") for key in ("country", "state", "city")}
		if not location["country"]:
			missing.append("customer_country")
	else:
		location = {key: shipping.get(key, "") for key in ("country", "state", "city")}
	country, region = _country(location["country"])
	if country:
		location["country"] = country
	# A real known customer country is useful even when their city or billing
	# street address is unknown. Never derive overseas geography from a forwarder.
	location["allow_country_only"] = bool(forwarder or explicit)
	return location, region


def normalize_profile(values: dict, existing=None) -> dict:
	"""Normalize complete profile input without writing or replacing existing data.

	``existing`` is an optional readable Customer Document/dict. Returned dictionaries
	contain only native fields; Contact child tables are ready for Document.append.
	Missing entries use input keys (billing keys have a ``billing_address.`` prefix).
	"""
	if not isinstance(values, dict):
		frappe.throw("客户资料必须为对象。")
	unknown = set(values) - INPUT_FIELDS
	if unknown:
		frappe.throw("不支持的客户资料字段：" + ", ".join(sorted(map(str, unknown))))
	normalized = {}
	for key, value in values.items():
		if key not in {"billing_address", "postal_code_not_applicable", "contact_phone_not_provided", "customer_country_not_provided"}:
			normalized[key] = _text(value, key, 4000 if key == "customer_details" else 140)
	normalized["postal_code_not_applicable"] = _flag(values, "postal_code_not_applicable")
	normalized["contact_phone_not_provided"] = _flag(values, "contact_phone_not_provided")
	normalized["customer_country_not_provided"] = _flag(values, "customer_country_not_provided")
	missing, warnings, defaults_used = [], [], {}
	policy = _address_policy(normalized, values, missing)
	customer = {key: normalized[key] for key in CUSTOMER_FIELDS if normalized.get(key)}
	manager = _account_manager(customer.get("account_manager"), existing, defaults_used, missing)
	if manager:
		customer["account_manager"] = manager
	for key in ("customer_name", "customer_type"):
		if not customer.get(key) and existing and existing.get(key):
			customer[key] = existing.get(key)
			defaults_used[key] = {"value": customer[key], "source": "existing_customer"}
		if not customer.get(key):
			missing.append(key)
	if customer.get("customer_type") and customer["customer_type"] not in {"Company", "Individual", "Partnership"}:
		frappe.throw("客户类型必须为 Company、Individual 或 Partnership。")
	for key, doctype in LINKS.items():
		if customer.get(key):
			customer[key] = _link(doctype, customer[key]).name
		elif key == "customer_group":
			value = existing.get(key) if existing else None
			source = "existing_customer" if value else "customer_type"
			if not value:
				value = {"Company": "Commercial", "Partnership": "Commercial", "Individual": "Individual"}.get(customer.get("customer_type"))
			if value:
				customer[key] = _link(doctype, value).name
				defaults_used[key] = {"value": customer[key], "source": source}
		elif key == "default_currency":
			value = _default(key, existing, defaults_used, warnings)
			if value:
				customer[key] = value
	if not customer.get("default_price_list") and existing and existing.get("default_price_list"):
		customer["default_price_list"] = _link("Price List", existing.get("default_price_list")).name
		defaults_used["default_price_list"] = {"value": customer["default_price_list"], "source": "existing_customer"}
	if not customer.get("default_currency") and customer.get("default_price_list"):
		currency = _link("Price List", customer["default_price_list"]).get("currency")
		customer["default_currency"] = _link("Currency", currency).name
		defaults_used["default_currency"] = {"value": currency, "source": "selected_price_list"}
	shipping_values = {key: value for key, value in normalized.items() if key not in {"mobile_no", "phone_no", "email_id"}}
	shipping_values["phone"] = normalized.get("shipping_phone", "")
	if policy["shipping_type"] == "Shipping":
		shipping_values["email_id"] = normalized.get("email_id", "")
	shipping, _ = _address(shipping_values, policy["shipping_type"], missing, warnings)
	if normalized.get("shipping_contact_name"):
		shipping["address_title"] = normalized["shipping_contact_name"]
	if policy["shipping_type"] == "货代收货" and not shipping.get("phone"):
		missing.append("shipping_phone")
	territory_location, region = _customer_location(normalized, shipping, policy, missing, warnings)
	for key in ("customer_group", "default_currency"):
		if not customer.get(key):
			missing.append(key)
	if customer.get("default_price_list") and customer.get("default_currency"):
		if _link("Price List", customer["default_price_list"]).get("currency") != customer["default_currency"]:
			frappe.throw("销售价目表币种与客户交易币种不一致，请确认后再建档。")
	if customer.get("website"):
		try:
			url = urlsplit(customer["website"])
		except ValueError:
			frappe.throw("客户网站地址格式无效。")
		if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
			frappe.throw("客户网站必须为有效的 http 或 https 地址。")
	contact_name = normalized.get("contact_name")
	if not contact_name and customer.get("customer_type") == "Individual":
		contact_name = customer.get("customer_name")
	if not contact_name:
		missing.append("contact_name")
	contact = {"first_name": contact_name} if contact_name else {}
	if customer.get("customer_type") in {"Company", "Partnership"} and customer.get("customer_name"):
		contact["company_name"] = customer["customer_name"]
	for source, target in (("contact_designation", "designation"), ("contact_department", "department")):
		if normalized.get(source):
			contact[target] = normalized[source]
	email = _email(normalized.get("email_id", ""), "email_id")
	if email:
		contact["email_ids"] = [{"email_id": email, "is_primary": 1}]
	else:
		warnings.append("未提供联系人邮箱，后续邮件报价、订单及账单通知可能无法使用。")
	phones = []
	for key in ("phone_no", "mobile_no"):
		phone = _phone(normalized.get(key), region, key, warnings,
			require_country_code=normalized["customer_country_not_provided"])
		if not phone:
			continue
		is_mobile = key == "mobile_no"
		row = next((item for item in phones if item["phone"] == phone["phone"]), None)
		if row is None:
			row = {"phone": phone["phone"], "is_primary_phone": 0, "is_primary_mobile_no": 0}
			phones.append(row)
		row["is_primary_mobile_no" if is_mobile else "is_primary_phone"] = 1
	if len([row for row in phones if row["is_primary_phone"]]) > 1:
		for row in phones[1:]:
			row["is_primary_phone"] = 0
	if phones:
		contact["phone_nos"] = phones
		if policy["shipping_type"] == "Shipping" and not normalized.get("shipping_phone"):
			shipping["phone"] = phones[0]["phone"]
	if normalized["contact_phone_not_provided"]:
		if policy["shipping_type"] != "货代收货":
			frappe.throw("只有明确货代收货且提供货代收货电话时，才可标记客户本人电话未提供。")
		if normalized.get("phone_no") or normalized.get("mobile_no"):
			frappe.throw("已提供客户电话，不能同时标记客户本人电话未提供。")
		warnings.append("客户本人电话未提供；货代收货电话仅保存到收货地址，不会写入客户联系人。")
	if not phones and not (normalized["contact_phone_not_provided"] and shipping.get("phone")):
		missing.append("mobile_no_or_phone_no")
	billing = None
	if values.get("billing_address") is not None:
		raw = values["billing_address"]
		if not isinstance(raw, dict):
			frappe.throw("billing_address 必须为地址对象或 null。")
		unknown = set(raw) - BILLING_FIELDS
		if unknown:
			frappe.throw("不支持的账单地址字段：" + ", ".join(sorted(map(str, unknown))))
		clean = {key: _text(value, "billing_address." + key) for key, value in raw.items() if key != "postal_code_not_applicable"}
		clean["postal_code_not_applicable"] = _flag(raw, "postal_code_not_applicable")
		billing, _ = _address(clean, "Billing", missing, warnings, "billing_address.")
	if policy["billing_mode"] == "not_provided":
		warnings.append("发票地址未提供：本次不建立发票地址，不把收货地址设为客户首选开票地址；已有真实发票地址保留。")
	return {"customer": customer, "contact": contact, "shipping": shipping, "billing": billing,
		"address_policy": policy, "territory_location": territory_location,
		"missing": list(dict.fromkeys(missing)), "warnings": list(dict.fromkeys(warnings)), "defaults_used": defaults_used}
