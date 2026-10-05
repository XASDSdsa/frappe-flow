"""Read-only, permission-aware input resolution for the Sales Order Flow tool.

The caller checks native Sales Order create permission before calling this module.
Only the current user's own User -> Employee -> Sales Person identity lookup is
internal; Customer, Item and UOM documents keep their normal read permission.
No prices, records, attributes or permission settings are written here.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import math

import frappe
from frappe.utils import nowdate


STICKER_TEMPLATE = "巧克粉贴纸"
STICKER_ATTRIBUTES = ("客户", "贴纸型号", "贴纸版本")
PRODUCT_FIELDS = {"item_code", "item_name", "qty", "uom", "rate", "standalone", "is_free_item"}
SERVICE_FIELDS = PRODUCT_FIELDS - {"standalone", "is_free_item"}
MAPPING_FIELDS = (PRODUCT_FIELDS - {"standalone", "is_free_item"}) | {"product_row", "sticker_model", "sticker_version"}
BUNDLE_MAPPING_FIELDS = {"product_row", "sticker_row"}
PRODUCT_SHORTHANDS = {f"山东{color}方" for color in "绿灰蓝粉"}
STANDALONE_STICKER_WARNING = (
	"本单含独立交付的客户专属贴纸，未绑定配套商品。请在本次整体审核中重点核对客户、型号版本、数量及用途；"
	"贴纸售价统一为 0 元，后续出库仍需真实库存和成本。如向客户收取一次性定制费，应另列定制服务费。无需另行确认。"
)


def _issue(issues, code, field, message, **details):
	issues.append({"code": code, "field": field, "message": message, **details})


def _text(value, field, issues, *, required=False):
	if value is None or value == "":
		if required:
			_issue(issues, "missing", field, f"请补充 {field}。")
		return None
	if not isinstance(value, str) or len(value) > 140 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
		_issue(issues, "invalid_text", field, f"{field} 必须是 140 字以内的有效文本。")
		return None
	value = value.strip()
	if not value:
		if required:
			_issue(issues, "missing", field, f"请补充 {field}。")
		return None
	return value


def _number(value, field, issues, *, allow_zero=False):
	"""Reject bool, nonfinite, underflow and overflow before native float fields."""
	try:
		if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
			raise ValueError
		result = Decimal(str(value))
		if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
			raise ValueError
		native = float(result)
		if not math.isfinite(native) or (result != 0 and native == 0):
			raise ValueError
		return result
	except (ValueError, TypeError, InvalidOperation, OverflowError):
		_issue(issues, "invalid_number", field, f"{field} 必须是{'非负' if allow_zero else '大于零的'}有限数字。")
		return None


def _native_number(value):
	return int(value) if value == value.to_integral_value() else float(value)


def _read(doctype, name, field, issues):
	try:
		doc = frappe.get_doc(doctype, name)
		doc.check_permission("read")
		return doc
	except frappe.PermissionError:
		_issue(issues, "permission_denied", field, f"当前用户无权读取所选{doctype}，请联系管理员核对访问权限。")
	except frappe.DoesNotExistError:
		_issue(issues, "not_found", field, f"所选{doctype}不存在或已删除，请重新选择。")
	return None


def _exact_names(doctype, field, value):
	# MariaDB text equality can ignore case/accents; verify the actual saved value.
	return [row["name"] for row in frappe.get_all(
		doctype, filters={field: value}, fields=list(dict.fromkeys(["name", field])), limit_page_length=0,
	) if row.get(field) == value]


def _readable(doctype, name):
	try:
		frappe.get_doc(doctype, name).check_permission("read")
		return True
	except (frappe.PermissionError, frappe.DoesNotExistError):
		return False


def _customer_choices(value):
	"""Readable enabled customers containing the typed text; never auto-selected."""
	found = {}
	for field in ("name", "customer_name"):
		for row in frappe.get_all("Customer", filters={field: ["like", f"%{value}%"], "disabled": 0},
			fields=["name", "customer_name"], limit_page_length=10):
			found.setdefault(row["name"], row)
	return [{"customer": name, "customer_name": row.get("customer_name")}
		for name, row in list(found.items())[:10] if _readable("Customer", name)]


def _item_choices(field, value):
	"""Readable enabled sales items containing the typed text, excluding generated bundles."""
	choices = []
	for row in frappe.get_all("Item", filters={field: ["like", f"%{value}%"], "disabled": 0, "has_variants": 0,
		"is_sales_item": 1}, fields=["name", "item_name"], limit_page_length=30):
		if row["name"].startswith("FLOW-COMBO-") or not _readable("Item", row["name"]):
			continue
		choices.append({"item_code": row["name"], "item_name": row.get("item_name")})
		if len(choices) == 10:
			break
	return choices


def _customer(value, issues):
	value = _text(value, "customer", issues, required=True)
	if not value:
		return None
	names = _exact_names("Customer", "name", value) or _exact_names("Customer", "customer_name", value)
	if len(names) != 1:
		_issue(issues, "ambiguous_customer" if names else "customer_not_found", "customer",
			"客户名称不唯一，请选择明确的客户编号。" if names else "没有找到完全匹配的客户，请从候选中选择客户编号。",
			choices=[{"customer": name} for name in names] if names else _customer_choices(value))
		return None
	doc = _read("Customer", names[0], "customer", issues)
	if doc and doc.get("disabled"):
		_issue(issues, "disabled_customer", "customer", "该客户已停用，请选择启用的客户。")
		return None
	return doc


def resolve_sales_identity(issues):
	"""Return only the session user's identity, without exposing HR documents."""
	user = frappe.session.user
	if not user or user == "Guest":
		_issue(issues, "invalid_session_user", "sales_person", "请使用已启用的系统用户登录后创建销售订单。")
		return user, []
	account = frappe.db.get_value("User", user, ["name", "enabled", "user_type"], as_dict=True)
	if not account or account.get("name") != user or not account.get("enabled") or account.get("user_type") != "System User":
		_issue(issues, "invalid_session_user", "sales_person", "当前登录账号必须是已启用的系统用户。")
		return user, []
	# These filters are fixed by the authenticated session, never by tool input.
	employees = frappe.get_all("Employee", filters={"user_id": user, "status": "Active"},
		fields=["name"], limit_page_length=2)
	if len(employees) != 1:
		_issue(issues, "employee_mapping", "sales_person",
			"当前用户需要且只能关联一位在职员工，请管理员核对员工的用户账号和在职状态。")
		return user, []
	people = frappe.get_all("Sales Person", filters={"employee": employees[0]["name"], "enabled": 1, "is_group": 0},
		fields=["name"], limit_page_length=2)
	if len(people) != 1:
		_issue(issues, "sales_person_mapping", "sales_person",
			"当前员工需要且只能关联一位启用的末级销售员，请管理员核对销售员的员工关联。")
		return user, []
	return user, [{"sales_person": people[0]["name"], "allocated_percentage": 100}]


def resolve_delivery(delivery_date, delivery_days, base_date, issues):
	try:
		base = base_date if base_date is not None else nowdate()
		if isinstance(base, datetime):
			raise ValueError
		base = base if isinstance(base, date) else date.fromisoformat(base)
		if delivery_date is not None and delivery_date != "":
			if isinstance(delivery_date, datetime):
				raise ValueError
			target = delivery_date if isinstance(delivery_date, date) else date.fromisoformat(delivery_date)
			if isinstance(delivery_date, str) and target.isoformat() != delivery_date:
				raise ValueError
		else:
			if type(delivery_days) is not int or delivery_days < 0:
				_issue(issues, "invalid_delivery_days", "delivery_days", "交期天数必须是非负整数，按自然日计算。")
				return None, None
			target = base + timedelta(days=delivery_days)
		if target < base:
			_issue(issues, "past_delivery_date", "delivery_date", "交货日期不能早于订单日期。")
			return None, None
		return target.isoformat(), target.isoformat() + " 23:59:00"
	except (ValueError, TypeError, OverflowError):
		_issue(issues, "invalid_delivery_date", "delivery_date", "请提供有效的 YYYY-MM-DD 交货日期。")
		return None, None


def _attributes(doc):
	pairs = [(row.get("attribute"), row.get("attribute_value")) for row in doc.get("attributes") or []]
	if len({key for key, _ in pairs}) != len(pairs):
		return None
	return dict(pairs)


def _shorthand_item(name, field, issues):
	"""Only four documented shorthand names, with no substring/fuzzy matching."""
	full_name = "山东中性" + name[2:]
	documents = []
	for row in frappe.get_all("Item", filters={"item_name": ["in", [name, full_name]],
		"disabled": 0, "has_variants": 0, "is_sales_item": 1}, fields=["name", "item_name"], limit_page_length=0):
		if row.get("item_name") not in {name, full_name} or row["item_name"].replace("中性", "") != name:
			continue
		try:
			doc = frappe.get_doc("Item", row["name"])
			doc.check_permission("read")
		except (frappe.PermissionError, frappe.DoesNotExistError):
			continue
		if (doc.get("disabled") or doc.get("has_variants") or not doc.get("is_sales_item")
			or doc.get("item_name") not in {name, full_name} or doc.get("variant_of") == STICKER_TEMPLATE):
			continue
		documents.append(doc)
	if len(documents) != 1:
		_issue(issues, "ambiguous_item" if documents else "item_not_found", field,
			"该商品简称对应多个商品，请选择具体商品。" if documents else "没有找到该简称对应的可读、启用销售商品，请核对完整商品名称。",
			choices=[{"item_code": doc.name, "item_name": doc.get("item_name")} for doc in documents])
		return None
	return documents[0]


def _item(values, field, issues, *, allow_product_shorthand=False):
	code = _text(values.get("item_code"), field + ".item_code", issues)
	name = _text(values.get("item_name"), field + ".item_name", issues)
	if not code and not name:
		_issue(issues, "missing_item", field, "请选择明确的商品编码或完整商品名称。")
		return None
	if allow_product_shorthand and not code and name in PRODUCT_SHORTHANDS:
		return _shorthand_item(name, field, issues)
	names = _exact_names("Item", "name" if code else "item_name", code or name)
	if len(names) != 1:
		_issue(issues, "ambiguous_item" if names else "item_not_found", field,
			"商品名称不唯一，请选择具体商品。" if names else "没有找到完全匹配的商品，请从候选中选择商品编码。",
			choices=[{"item_code": item} for item in names] if names else _item_choices("name" if code else "item_name", code or name))
		return None
	doc = _read("Item", names[0], field, issues)
	if not doc:
		return None
	if code and name and name != doc.get("item_name"):
		_issue(issues, "item_identity_conflict", field, "商品编码与商品名称指向不一致，请核对。")
	if doc.get("disabled"):
		_issue(issues, "disabled_item", field, "所选商品已停用，请选择启用的商品。")
	if doc.get("has_variants"):
		_issue(issues, "item_template", field, "所选物料是模板，请选择明确的颜色或其他规格变体。")
	if not doc.get("is_sales_item"):
		_issue(issues, "not_sales_item", field, "所选物料未启用销售用途，请管理员核对。")
	return doc


def _conversion_factor(item, uom, field, issues):
	if uom == item.get("stock_uom"):
		return Decimal(1)
	for source in (item,):
		for row in source.get("uoms") or []:
			if row.get("uom") == uom:
				return _number(row.get("conversion_factor"), field, issues)
	if item.get("variant_of"):
		template = _read("Item", item.get("variant_of"), field, issues)
		if template:
			for row in template.get("uoms") or []:
				if row.get("uom") == uom:
					return _number(row.get("conversion_factor"), field, issues)
	# Follow native global direct/inverse/intermediate UOM conversion. Unlike the
	# outer native helper, do not silently turn a missing conversion into 1:1.
	from erpnext.stock.doctype.item.item import get_uom_conv_factor
	value = get_uom_conv_factor(uom, item.get("stock_uom"))
	if value is None:
		_issue(issues, "missing_uom_conversion", field, "所选单位缺少到库存单位的换算，请选择已配置的单位。")
		return None
	return _number(value, field, issues)


def _quantity(item, values, field, issues, *, default_qty=None):
	qty = _number(values.get("qty", default_qty), field + ".qty", issues)
	uom = _text(values.get("uom"), field + ".uom", issues) or item.get("sales_uom") or item.get("stock_uom")
	if not uom or not item.get("stock_uom"):
		_issue(issues, "missing_uom", field + ".uom", "商品缺少销售单位或库存单位，请管理员补齐。")
		return None
	uom_doc = _read("UOM", uom, field + ".uom", issues)
	stock_doc = uom_doc if uom == item.get("stock_uom") else _read("UOM", item.get("stock_uom"), field + ".uom", issues)
	factor = _conversion_factor(item, uom, field + ".uom", issues)
	if qty is None or factor is None or not uom_doc or not stock_doc:
		return None
	stock_qty = qty * factor
	if uom_doc.get("must_be_whole_number") and qty != qty.to_integral_value():
		_issue(issues, "whole_uom_required", field + ".qty", "所选单位要求整数数量，请调整数量或单位。")
	if stock_doc.get("must_be_whole_number") and stock_qty != stock_qty.to_integral_value():
		_issue(issues, "whole_stock_uom_required", field + ".qty", "换算后的库存数量必须为整数，请调整数量或单位。")
	if not math.isfinite(float(stock_qty)):
		_issue(issues, "invalid_stock_quantity", field + ".qty", "换算后的库存数量超出有效范围，请调整数量。")
		return None
	return {"qty": _native_number(qty), "uom": uom, "stock_uom": item.get("stock_uom"),
		"conversion_factor": _native_number(factor), "stock_qty": _native_number(stock_qty)}


def _sticker_details(item, customer, field, issues):
	if not item.get("is_stock_item"):
		_issue(issues, "sticker_not_stock_item", field, "客户贴纸必须启用库存管理，请管理员先修正物料档案。")
		return None
	attrs = _attributes(item)
	legacy = _legacy_sticker_details(item, customer)
	if legacy:
		return legacy
	if item.get("variant_of") != STICKER_TEMPLATE or not attrs or set(attrs) != set(STICKER_ATTRIBUTES) or not all(attrs.values()):
		_issue(issues, "invalid_sticker", field, "请选择客户、贴纸型号、贴纸版本完整的巧克粉贴纸变体。")
		return None
	if customer and attrs["客户"] != customer.get("customer_name"):
		_issue(issues, "sticker_customer_mismatch", field, "所选贴纸的客户属性与本订单客户不一致，请重新选择。")
		return None
	return {"sticker_model": attrs["贴纸型号"], "sticker_version": attrs["贴纸版本"]}


def _legacy_sticker_details(item, customer):
	"""Existing legacy stickers have no variant attributes; never override any.

	Only the exact unique customer name prefix plus nonempty model/version is
	accepted. The caller verifies customer-name uniqueness before using any row.
	"""
	if not customer or item.get("variant_of") or item.get("attributes"):
		return None
	prefix = STICKER_TEMPLATE + "-"
	if not item.name.startswith(prefix):
		return None
	# The verified legacy suffix is exactly model-version. Splitting from the
	# right preserves hyphens in customer names and prevents customer "A" from
	# claiming a sticker owned by customer "A-B" as model "B-...".
	parts = item.name[len(prefix):].rsplit("-", 2)
	if len(parts) != 3 or parts[0] != customer.get("customer_name") or not all(part and part == part.strip() for part in parts):
		return None
	return {"sticker_model": parts[1], "sticker_version": parts[2], "sticker_identity_source": "legacy_exact_customer_prefix"}


def _model_key(model):
	from erpnext.stock.doctype.item.sticker_models import normalize_model

	try:
		return normalize_model(model)
	except ValueError:
		pass
	return model[:-2] + "模版" if model and model.endswith("模板") else model


def _catalog_details(item, customer):
	legacy = _legacy_sticker_details(item, customer)
	if legacy:
		return legacy
	attrs = _attributes(item)
	if item.get("variant_of") == STICKER_TEMPLATE and attrs and set(attrs) == set(STICKER_ATTRIBUTES) and all(attrs.values()) and attrs.get("客户") == customer.get("customer_name"):
		return {"sticker_model": attrs["贴纸型号"], "sticker_version": attrs["贴纸版本"]}
	return None


def _sticker_catalog(customer, issues):
	if not customer:
		return []
	if len(_exact_names("Customer", "customer_name", customer.get("customer_name"))) != 1:
		_issue(issues, "ambiguous_sticker_owner", "customer", "多个客户使用相同完整名称，现有贴纸客户属性无法区分，请管理员先整理客户名称。")
		return []
	# Prefer saved attributes; support only the verified legacy naming convention.
	candidates = []
	filters = {"disabled": 0, "has_variants": 0, "is_sales_item": 1}
	names = []
	for selector in ({"variant_of": STICKER_TEMPLATE}, {"name": ["like", STICKER_TEMPLATE + "-" + customer.get("customer_name") + "-%"]}):
		names += [row["name"] for row in frappe.get_all("Item", filters={**filters, **selector}, fields=["name"], limit_page_length=0)]
	for name in dict.fromkeys(names):
		try:
			item = frappe.get_doc("Item", name)
			item.check_permission("read")
		except (frappe.PermissionError, frappe.DoesNotExistError):
			continue
		if _catalog_details(item, customer):
			candidates.append(item)
	return candidates


def _choice(item, customer):
	return {"item_code": item.name, "item_name": item.get("item_name"),
		**_catalog_details(item, customer)}


def first_order_options(customer):
	"""Saved sticker versions and readable sticker-making services for one first-order question."""
	doc = frappe.get_doc("Customer", customer)
	stickers = [_choice(item, doc) for item in _sticker_catalog(doc, [])]
	services = []
	for row in frappe.get_all("Item", filters={"disabled": 0, "has_variants": 0, "is_sales_item": 1, "is_stock_item": 0,
		"item_name": ["like", "%贴纸%"]}, fields=["name", "item_name"], limit_page_length=50):
		if (row["name"].startswith("FLOW-COMBO-") or frappe.db.exists("Product Bundle", {"new_item_code": row["name"]})
			or not _readable("Item", row["name"])):
			continue
		services.append({"item_code": row["name"], "item_name": row.get("item_name")})
	return {"stickers": stickers, "services": services[:10]}


def _product_sticker_model(item):
	# An explicit model attribute is evidence; a color or partial name is not.
	attrs = _attributes(item) or {}
	return attrs.get("贴纸型号") or ((item.get("item_name") + "模版") if item.get("item_name") else None)


def _free_sticker_rate(values, field, issues):
	if "rate" in values:
		rate = _number(values["rate"], field + ".rate", issues, allow_zero=True)
		if rate is not None and rate != 0:
			_issue(issues, "sticker_must_be_free", field + ".rate",
				"客户贴纸售价统一为 0 元；如需向客户收取一次性定制费，请另列定制服务费，不修改贴纸单价。")
	return Decimal(0)


def _reject_bundle_service(item, field, issues):
	# Non-stock bundle parents still deliver goods, even if their bundle is disabled.
	if not item.get("is_stock_item") and frappe.db.exists("Product Bundle", {"new_item_code": item.name}):
		_issue(issues, "bundle_not_service", field, "组合商品不能作为定制服务，请通过商品行选择。")


def _free_service_rate(item, values, field, issues):
	"""Allow an explicitly free non-stock service row only."""
	_reject_bundle_service(item, field, issues)
	if item.get("is_stock_item"):
		_issue(issues, "free_stock_item_not_allowed", field + ".is_free_item",
			"库存商品不能标记为免费服务。")
	if "rate" not in values:
		_issue(issues, "free_service_rate_required", field + ".rate",
			"免费服务行必须明确传入单价 0，不能从物料名称或默认价格推断。")
	rate = _number(values.get("rate"), field + ".rate", issues, allow_zero=True)
	if rate is not None and rate != 0:
		_issue(issues, "free_service_rate_must_be_zero", field + ".rate",
			"已标记为免费服务时，单价必须为 0。")
	return Decimal(0) if rate == 0 else None


def _same_stock_quantity(product, sticker):
	return Decimal(str(product["stock_qty"])) == Decimal(str(sticker["stock_qty"]))


def _paired_input_row(product, sticker):
	return {**sticker, "row_type": "sticker", "input_sticker_row": sticker["product_row"],
		"product_row": product["product_row"], "product_item_code": product["item_code"],
		"bundle_eligible": True, "rate": 0, "is_free_item": 1}


def _existing_bundle_pairs(product_docs, standalone_rows, bundle_mappings, legacy_product_rows, issues):
	"""Pair existing input rows, consuming each source row at most once.

	Natural-language inference belongs to the agent. This fallback accepts only
	mutually unique exact model matches, never list position or color alone.
	"""
	if bundle_mappings is None:
		bundle_mappings = []
	elif not isinstance(bundle_mappings, list):
		_issue(issues, "invalid_bundle_mappings", "bundle_mappings", "组合对应关系必须为列表。")
		bundle_mappings = []
	sticker_by_row = {row["product_row"]: row for row in standalone_rows}
	paired, used_products, used_stickers = [], set(), set()
	for index, values in enumerate(bundle_mappings, 1):
		field = f"bundle_mappings[{index}]"
		before = len(issues)
		if not isinstance(values, dict):
			_issue(issues, "invalid_bundle_mapping", field, "每条组合对应关系必须包含原始商品行号和贴纸行号。")
			continue
		if set(values) - BUNDLE_MAPPING_FIELDS:
			_issue(issues, "unsupported_fields", field, "组合对应关系只接受 product_row 和 sticker_row。")
		product_index, sticker_index = values.get("product_row"), values.get("sticker_row")
		product = product_docs.get(product_index) if type(product_index) is int else None
		sticker = sticker_by_row.get(sticker_index) if type(sticker_index) is int else None
		if not product:
			_issue(issues, "invalid_product_row", field + ".product_row", "请选择有效的原始商品行号（从 1 开始）。")
		if not sticker:
			_issue(issues, "invalid_sticker_row", field + ".sticker_row", "请选择有效的原始客户贴纸行号（从 1 开始）。")
		if not product or not sticker:
			continue
		if sticker.get("standalone"):
			_issue(issues, "standalone_bundle_conflict", field, "该贴纸已明确为独立交付或备用，不能同时作为组合贴纸。")
		if product_index in used_products or sticker_index in used_stickers or product_index in legacy_product_rows:
			_issue(issues, "duplicate_bundle_mapping", field, "同一商品行或贴纸行只能用于一个组合，不可重复或与 sticker_mappings 同时指定。")
		used_products.add(product_index)
		used_stickers.add(sticker_index)
		if not _same_stock_quantity(product[0], sticker):
			_issue(issues, "bundle_quantity_mismatch", field,
				"组合要求每件商品配一张贴纸；当前实物数量不一致，请明确拆分组合数量和剩余独立交付数量。",
				product_row=product_index, sticker_row=sticker_index,
				product_stock_qty=product[0]["stock_qty"], sticker_stock_qty=sticker["stock_qty"])
		if len(issues) == before:
			paired.append(_paired_input_row(product[0], sticker))
	available_products = {key: value for key, value in product_docs.items()
		if key not in used_products and key not in legacy_product_rows}
	available_stickers = {key: value for key, value in sticker_by_row.items()
		if key not in used_stickers and not value.get("standalone")}
	candidates = {key: [sticker_key for sticker_key, sticker in available_stickers.items()
		if _model_key(_product_sticker_model(value[1])) == _model_key(sticker["sticker_model"])
		and _same_stock_quantity(value[0], sticker)]
		for key, value in available_products.items()}
	for product_index, choices in candidates.items():
		if len(choices) != 1:
			continue
		sticker_index = choices[0]
		if sum(sticker_index in values for values in candidates.values()) != 1:
			continue
		product, sticker = available_products[product_index][0], available_stickers[sticker_index]
		if not _same_stock_quantity(product, sticker):
			continue
		paired.append(_paired_input_row(product, sticker))
		used_products.add(product_index)
		used_stickers.add(sticker_index)
	# An unresolved co-occurring sticker needs a deliberate mapping or an explicit
	# standalone marker. Never silently discard it or manufacture another copy.
	unresolved = []
	for sticker_index, sticker in available_stickers.items():
		if sticker_index in used_stickers or not product_docs:
			continue
		matches = [key for key, values in candidates.items() if sticker_index in values]
		choices = matches or list(product_docs)
		unresolved.append({"sticker_row": sticker_index, "item_code": sticker["item_code"],
			"sticker_model": sticker["sticker_model"], "sticker_version": sticker["sticker_version"],
			"stock_qty": sticker["stock_qty"], "product_candidates": [
				{"product_row": key, "item_code": product_docs[key][0]["item_code"],
				 "item_name": product_docs[key][0]["item_name"], "stock_qty": product_docs[key][0]["stock_qty"]}
				for key in choices]})
	if unresolved:
		_issue(issues, "bundle_mapping_required", "bundle_mappings",
			"商品与贴纸的对应关系或数量不唯一。请结合本轮需求明确对应行；只有仍有歧义时询问客服。"
			"每件商品配一张贴纸，数量不等须明确拆分；独立交付或备用贴纸标记 standalone=true。", choices=unresolved)
	return paired, used_products, {row["input_sticker_row"] for row in paired}


def resolve_order_inputs(customer, items, company=None, currency=None, delivery_date=None,
	delivery_days=7, stickers_free=True, sticker_mappings=None, include_stickers=False, base_date=None, bundle_mappings=None,
	customization_services=None):
	"""Collect all actionable input problems, returning no executable rows on error.

	Item rows accept item_code/item_name, qty, optional uom, rate and standalone.
	Every customer sticker has zero sale rate; standalone stickers remain valid.
	Bundle mappings connect original one-based product/sticker input rows. Sticker mappings
	accept a one-based product_row plus an exact item_code/item_name or exact model
	and version. Omitted sticker qty means the associated product's stock quantity;
	this is the number of physical goods even when sold in a different sales UOM.
	Every paired sticker has the same physical quantity as its product and is eligible
	for one native sales bundle. This resolver does not create bundle definitions.
	Explicit customization services use existing non-stock sales items, defaulting
	to one unit at zero sale price. Stickers alone never imply a new service.
	"""
	issues, warnings = [], []
	user, sales_team = resolve_sales_identity(issues)
	customer_doc = _customer(customer, issues)
	company = _text(company, "company", issues)
	currency = _text(currency, "currency", issues)
	date_value, deadline = resolve_delivery(delivery_date, delivery_days, base_date, issues)
	if type(stickers_free) is not bool:
		_issue(issues, "invalid_flag", "stickers_free", "stickers_free 必须是 true 或 false。")
	elif not stickers_free:
		_issue(issues, "sticker_must_be_free", "stickers_free", "客户贴纸售价统一为 0 元；一次性定制费请另列服务费，stickers_free 必须为 true。")
	if type(include_stickers) is not bool:
		_issue(issues, "invalid_flag", "include_stickers", "include_stickers 必须是 true 或 false。")
	if not isinstance(items, list) or not items:
		_issue(issues, "missing_items", "items", "请提供至少一行商品及数量。")
		items = []
	product_rows, standalone_sticker_rows, input_rows, product_docs = [], [], [], {}
	sticker_customer_checked = False
	for index, values in enumerate(items, 1):
		field = f"items[{index}]"
		before = len(issues)
		if not isinstance(values, dict):
			_issue(issues, "invalid_item_row", field, "每行商品必须是包含商品和数量的对象。")
			continue
		unknown = set(values) - PRODUCT_FIELDS
		if unknown:
			_issue(issues, "unsupported_fields", field, "商品行包含不支持的字段：" + ", ".join(sorted(map(str, unknown))))
		item = _item(values, field, issues, allow_product_shorthand=True)
		if not item:
			_number(values.get("qty"), field + ".qty", issues)
			continue
		is_sticker = item.get("variant_of") == STICKER_TEMPLATE or item.name.startswith(STICKER_TEMPLATE + "-")
		if "is_free_item" in values and type(values["is_free_item"]) is not bool:
			_issue(issues, "invalid_flag", field + ".is_free_item", "is_free_item 必须是 true 或 false。")
		if is_sticker:
			rate = _free_sticker_rate(values, field, issues)
		elif values.get("is_free_item") is True:
			rate = _free_service_rate(item, values, field, issues)
		else:
			rate = _number(values.get("rate"), field + ".rate", issues) if "rate" in values else None
		if "standalone" in values and type(values["standalone"]) is not bool:
			_issue(issues, "invalid_flag", field + ".standalone", "standalone 必须为 true 或 false。")
		if values.get("standalone") is True and not is_sticker:
			_issue(issues, "invalid_standalone_item", field + ".standalone", "standalone 仅用于明确独立交付或备用的客户贴纸。")
		details = {}
		if is_sticker:
			if customer_doc and not sticker_customer_checked:
				sticker_customer_checked = True
				if len(_exact_names("Customer", "customer_name", customer_doc.get("customer_name"))) != 1:
					_issue(issues, "ambiguous_sticker_owner", "customer", "多个客户使用相同完整名称，现有贴纸客户属性无法区分，请管理员先整理客户名称。")
			details = _sticker_details(item, customer_doc, field, issues)
		quantity = _quantity(item, values, field, issues)
		if quantity and len(issues) == before:
			row = {"product_row": index, "row_type": "standalone_sticker" if is_sticker else "product",
				"item_code": item.name, "item_name": item.get("item_name"), **details,
				**quantity, "delivery_date": date_value}
			if rate is not None:
				row["rate"] = _native_number(rate)
			input_rows.append(row)
			if is_sticker:
				row["is_free_item"] = 1
				if values.get("standalone") is True:
					row["standalone"] = True
				standalone_sticker_rows.append(row)
			elif values.get("is_free_item") is True:
				row["row_type"] = "service"
				row["is_free_item"] = 1
			else:
				product_rows.append(row)
				product_docs[index] = (row, item)
	if customization_services is None:
		customization_services = []
	elif not isinstance(customization_services, list) or len(customization_services) > 100:
		_issue(issues, "invalid_customization_services", "customization_services", "定制服务必须是最多 100 行的列表。")
		customization_services = []
	input_item_codes = {row["item_code"] for row in input_rows}
	for index, values in enumerate(customization_services, 1):
		field = f"customization_services[{index}]"
		before = len(issues)
		if not isinstance(values, dict):
			_issue(issues, "invalid_service_row", field, "每行定制服务必须是包含服务物料的对象。")
			continue
		unknown = set(values) - SERVICE_FIELDS
		if unknown:
			_issue(issues, "unsupported_fields", field, "定制服务包含不支持的字段：" + ", ".join(sorted(map(str, unknown))))
		rate = _number(values.get("rate", 0), field + ".rate", issues, allow_zero=True)
		item = _item(values, field, issues)
		if not item:
			continue
		if (item.get("is_stock_item") or item.get("variant_of") == STICKER_TEMPLATE
			or item.name == STICKER_TEMPLATE or item.name.startswith(STICKER_TEMPLATE + "-")):
			_issue(issues, "invalid_customization_service", field, "定制服务必须选择非库存销售服务物料，不能使用商品或贴纸。")
		_reject_bundle_service(item, field, issues)
		if item.name in input_item_codes:
			_issue(issues, "duplicate_service_input", field, "同一服务重复列出，请只保留一行并明确数量，避免重复计费。")
		input_item_codes.add(item.name)
		quantity = _quantity(item, values, field, issues, default_qty=1)
		if quantity and len(issues) == before:
			input_rows.append({"row_type": "service", "item_code": item.name, "item_name": item.get("item_name"),
				**quantity, "delivery_date": date_value, "rate": _native_number(rate), "is_free_item": int(rate == 0)})
	if sticker_mappings is None:
		sticker_mappings = []
	elif not isinstance(sticker_mappings, list):
		_issue(issues, "invalid_sticker_mappings", "sticker_mappings", "贴纸对应关系必须为列表。")
		sticker_mappings = []
	wants_stickers = bool(sticker_mappings) or include_stickers is True
	catalog = _sticker_catalog(customer_doc, issues) if wants_stickers else []
	mappings = list(sticker_mappings)
	explicit_rows = {entry.get("product_row") for entry in mappings if isinstance(entry, dict) and type(entry.get("product_row")) is int}
	sticker_rows, paired_products, consumed_stickers = _existing_bundle_pairs(
		product_docs, standalone_sticker_rows, bundle_mappings, explicit_rows, issues)
	if include_stickers is True:
		mappings += [{"product_row": index} for index in product_docs if index not in explicit_rows and index not in paired_products]
	seen_mappings = set()
	for index, values in enumerate(mappings, 1):
		field = f"sticker_mappings[{index}]"
		before = len(issues)
		if not isinstance(values, dict):
			_issue(issues, "invalid_sticker_mapping", field, "每条贴纸对应关系必须是对象。")
			continue
		unknown = set(values) - MAPPING_FIELDS
		if unknown:
			_issue(issues, "unsupported_fields", field, "贴纸对应关系包含不支持的字段：" + ", ".join(sorted(map(str, unknown))))
		product_index = values.get("product_row")
		product = product_docs.get(product_index) if type(product_index) is int else None
		if not product:
			_issue(issues, "invalid_product_row", field + ".product_row", "请选择有效的商品行号（从 1 开始）；如商品行有问题，请先修正该商品。")
		model = _text(values.get("sticker_model"), field + ".sticker_model", issues)
		version = _text(values.get("sticker_version"), field + ".sticker_version", issues)
		rate = _free_sticker_rate(values, field, issues)
		if values.get("item_code") or values.get("item_name"):
			item = _item(values, field, issues)
		else:
			model = model or (_product_sticker_model(product[1]) if product else None)
			matches = [doc for doc in catalog if (not model or _model_key(_catalog_details(doc, customer_doc)["sticker_model"]) == _model_key(model))
				and (not version or _catalog_details(doc, customer_doc)["sticker_version"] == version)]
			if not model or len(matches) != 1:
				_issue(issues, "sticker_choice_required" if matches else "sticker_not_found", field,
					"请选择该商品对应的贴纸型号和版本。" if matches else "未找到符合条件的可读、启用客户贴纸，请核对型号和版本或先补齐贴纸。",
					product_row=product_index, choices=[_choice(doc, customer_doc) for doc in (matches or catalog)])
				item = None
			else:
				item = matches[0]
		if item:
			mapping_key = (product_index if type(product_index) is int else None, item.name)
			if mapping_key in seen_mappings:
				_issue(issues, "duplicate_sticker_mapping", field, "同一商品行重复选择了同一贴纸，请合并数量后保留一条对应关系。")
			elif any(key[0] == mapping_key[0] for key in seen_mappings):
				_issue(issues, "multiple_stickers_for_product", field, "每个组合商品行只能对应一种贴纸；请先拆分商品行，再分别指定贴纸。")
			seen_mappings.add(mapping_key)
			details = _sticker_details(item, customer_doc, field, issues)
			if details and ((model and _model_key(details["sticker_model"]) != _model_key(model)) or (version and details["sticker_version"] != version)):
				_issue(issues, "sticker_identity_conflict", field, "所选贴纸与指定的型号或版本不一致，请核对。")
			quantity_values = dict(values)
			if "qty" not in values:
				# One sticker per physical product; use stock UOM to avoid inheriting a
				# sales pack conversion and accidentally doubling the sticker count.
				quantity_values.setdefault("uom", item.get("stock_uom"))
			quantity = _quantity(item, quantity_values, field, issues, default_qty=product[0]["stock_qty"] if product else None)
			if quantity and product and not _same_stock_quantity(product[0], quantity):
				_issue(issues, "bundle_quantity_mismatch", field, "组合要求每件商品配一张贴纸；请明确拆分组合数量和剩余独立交付数量。",
					product_row=product_index, product_stock_qty=product[0]["stock_qty"], sticker_stock_qty=quantity["stock_qty"])
			if details and quantity and product and customer_doc and len(issues) == before:
				row = {"row_type": "sticker", "product_row": product_index, "product_item_code": product[0]["item_code"],
					"item_code": item.name, "item_name": item.get("item_name"), **details, **quantity, "delivery_date": date_value,
					"is_free_item": 1, "rate": 0, "bundle_eligible": True}
				sticker_rows.append(row)
		elif "qty" in values:
			_number(values.get("qty"), field + ".qty", issues)
	customer_result = {key: customer_doc.get(key) for key in (
		"name", "customer_name", "customer_group", "territory", "default_currency", "default_price_list",
	)} if customer_doc else None
	standalone_sticker_rows = [row for row in standalone_sticker_rows if row["product_row"] not in consumed_stickers]
	input_rows = [row for row in input_rows if row["row_type"] != "standalone_sticker" or row["product_row"] not in consumed_stickers]
	sticker_rows.sort(key=lambda row: row["product_row"])
	if standalone_sticker_rows:
		warnings.append({"code": "standalone_customer_stickers", "severity": "warning",
			"item_codes": list(dict.fromkeys(row["item_code"] for row in standalone_sticker_rows)),
			"message": STANDALONE_STICKER_WARNING})
	return {"status": "needs_input" if issues else "ready", "issues": issues, "warnings": warnings,
		"missing": list(dict.fromkeys(issue["field"] for issue in issues)),
		"customer": customer_result, "company": company, "currency": currency,
		"session_user": user, "sales_team": sales_team, "delivery_date": date_value, "delivery_deadline": deadline,
		"product_rows": product_rows, "sticker_rows": sticker_rows, "standalone_sticker_rows": standalone_sticker_rows,
		"items": [] if issues else input_rows + sticker_rows}
