"""Permission-aware native sales bundles for one product and its customer sticker.

No commit or independent transaction is performed here. The reviewed order flow
owns the transaction, including rolling temporary preview masters back. Prices
remain the caller's native product prices; no Item Price records are copied.
"""

from decimal import Decimal, InvalidOperation
from copy import deepcopy
from hashlib import sha256
from html import escape
import json

import frappe


STICKER_TEMPLATE = "巧克粉贴纸"
STICKER_ATTRIBUTES = {"客户", "贴纸型号", "贴纸版本"}
DEFAULT_FIELDS = (
	"company", "default_warehouse", "selling_cost_center", "income_account",
	"default_discount_account", "default_cogs_account",
)
TAX_FIELDS = ("item_tax_template", "tax_category", "valid_from", "maximum_net_rate", "minimum_net_rate")
PROFILE_FIELDS = ("item_group", "brand", "stock_uom", "sales_uom", "grant_commission")


class BundleInputError(ValueError):
	"""The reviewed pairing cannot safely create or reuse this bundle."""


def _read(doctype, name):
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	return doc


def _hash(value):
	return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
		separators=(",", ":")).encode()).hexdigest()


def bundle_code(customer, product_code, sticker_code):
	"""The complete customer/product/sticker identities determine the master code."""
	return "FLOW-COMBO-" + _hash(["v1", customer, product_code, sticker_code])[:24]


def _one(value):
	try:
		return not isinstance(value, bool) and Decimal(str(value)) == 1
	except (InvalidOperation, ValueError, TypeError):
		return False


def _source_documents(product_code, sticker_code, customer):
	customer_name = customer if isinstance(customer, str) else customer.get("name")
	customer_doc = _read("Customer", customer_name)
	product, sticker = _read("Item", product_code), _read("Item", sticker_code)
	if customer_doc.get("disabled"):
		raise BundleInputError("订单客户已停用，不能创建组合商品。")
	for item in (product, sticker):
		if (not item.get("is_stock_item") or item.get("disabled") or item.get("has_variants")
			or not item.get("is_sales_item") or item.get("is_fixed_asset") or not item.get("stock_uom")):
			raise BundleInputError("组合内商品和贴纸必须是启用销售、启用库存且有库存单位的具体物料。")
		if item.get("delivered_by_supplier") or item.get("enable_deferred_revenue"):
			raise BundleInputError("供应商直送或递延收入物料暂不支持自动组合，请按原物料开单。")
		if frappe.db.exists("Product Bundle", {"new_item_code": item.name, "disabled": 0}):
			raise BundleInputError("组合内不能再嵌套销售组合。")
	if product.name == sticker.name or product.get("variant_of") == STICKER_TEMPLATE or product.name.startswith(STICKER_TEMPLATE + "-"):
		raise BundleInputError("组合必须由一件普通商品和一张客户贴纸组成。")
	# Sticker customer attributes use the display name. Duplicated customer names
	# cannot safely authorize ownership, even if this user can read only one.
	owner = customer_doc.get("customer_name")
	owners = [row["name"] for row in frappe.get_all("Customer", filters={"customer_name": owner},
		fields=["name", "customer_name"], limit_page_length=0) if row.get("customer_name") == owner]
	if owners != [customer_doc.name]:
		raise BundleInputError("客户完整名称不唯一，无法安全确认贴纸归属，请先整理客户档案。")
	pairs = [(row.get("attribute"), row.get("attribute_value")) for row in sticker.get("attributes") or []]
	attrs = dict(pairs)
	valid_variant = (sticker.get("variant_of") == STICKER_TEMPLATE and len(pairs) == len(attrs)
		and set(attrs) == STICKER_ATTRIBUTES and all(attrs.values()) and attrs.get("客户") == owner)
	legacy_parts = sticker.name[len(STICKER_TEMPLATE) + 1:].rsplit("-", 2)
	valid_legacy = (not sticker.get("variant_of") and not pairs
		and sticker.name.startswith(STICKER_TEMPLATE + "-") and len(legacy_parts) == 3
		and legacy_parts[0] == owner and all(part and part == part.strip() for part in legacy_parts))
	if not (valid_variant or valid_legacy):
		raise BundleInputError("贴纸客户、型号或版本不匹配，不能为该客户创建组合商品。")
	return product, sticker, customer_doc


def _rows(doc, field, fields):
	return [{key: row.get(key) for key in fields if row.get(key) is not None}
		for row in doc.get(field) or []]


def _profile(item):
	"""Sales-relevant master data only, with the same variant fallbacks as native."""
	profile = {field: item.get(field) or (0 if field == "grant_commission" else "") for field in PROFILE_FIELDS}
	profile["sales_uom"] = item.get("sales_uom") or item.get("stock_uom")
	uoms = _rows(item, "uoms", ("uom", "conversion_factor"))
	taxes = _rows(item, "taxes", TAX_FIELDS)
	if item.get("variant_of") and (not uoms or not taxes):
		template = _read("Item", item.get("variant_of"))
		uoms = uoms or _rows(template, "uoms", ("uom", "conversion_factor"))
		taxes = taxes or _rows(template, "taxes", TAX_FIELDS)
	if not any(row.get("uom") == item.get("stock_uom") for row in uoms):
		uoms.append({"uom": item.get("stock_uom"), "conversion_factor": 1})
	profile["uoms"] = sorted(uoms, key=lambda row: row.get("uom") or "")
	profile["item_defaults"] = _rows(item, "item_defaults", DEFAULT_FIELDS)
	profile["taxes"] = taxes
	return profile


def _normalized(value):
	# Native Float fields may read back as 1.0 and empty values as ""/None.
	if isinstance(value, dict):
		return {key: _normalized(val) for key, val in value.items() if val not in (None, "")}
	if isinstance(value, list):
		return [_normalized(val) for val in value]
	if isinstance(value, (int, float, Decimal)):
		return str(Decimal(str(value)).normalize())
	return value


def _compatible(parent, bundle, product, sticker, profile):
	if (bundle.get("disabled") or bundle.get("new_item_code") != parent.name
		or parent.get("disabled") or parent.get("has_variants") or parent.get("variant_of")
		or parent.get("is_stock_item") or parent.get("is_fixed_asset") or not parent.get("is_sales_item")
		or parent.get("delivered_by_supplier") or parent.get("enable_deferred_revenue")):
		return False
	rows = bundle.get("items") or []
	if len(rows) != 2 or {row.get("item_code") for row in rows} != {product.name, sticker.name}:
		return False
	units = {product.name: product.get("stock_uom"), sticker.name: sticker.get("stock_uom")}
	if any(not _one(row.get("qty")) or row.get("uom") != units[row.get("item_code")] for row in rows):
		return False
	return _normalized(_profile(parent)) == _normalized(profile)


def _result(parent, bundle, product, sticker, customer, disposition, profile):
	components = [{"item_code": item.name, "item_name": item.get("item_name") or item.name,
		"qty": 1, "uom": item.get("stock_uom")} for item in (product, sticker)]
	return {"item_code": parent.name, "item_name": parent.get("item_name") or parent.name,
		"components": components, "disposition": disposition,
		"signature": _hash(_normalized({"customer": customer.name, "item_code": parent.name,
			"item_name": parent.get("item_name"), "components": components, "profile": profile}))}


def _existing(code, product, sticker, customer, profile):
	if not (frappe.db.exists("Item", code) and frappe.db.exists("Product Bundle", code)):
		raise BundleInputError("审核的组合编码已存在但档案不完整，或已被删除，请重新审核。")
	parent, bundle = _read("Item", code), _read("Product Bundle", code)
	if not _compatible(parent, bundle, product, sticker, profile):
		raise BundleInputError("组合编码的物料、数量、单位或销售设置已不匹配，请重新审核，不能覆盖原组合。")
	return _result(parent, bundle, product, sticker, customer, "reuse", profile)


def ensure_bundle(product_code, sticker_code, customer, *, planned_code=None):
	"""Reuse one exact readable native bundle, or insert a deterministic new one.

	The caller must roll its whole savepoint back on any error. In particular,
	DuplicateEntryError remains visible during simultaneous creation, allowing the
	caller to request a fresh preview instead of committing partial master data.
	"""
	product, sticker, customer_doc = _source_documents(product_code, sticker_code, customer)
	profile = _profile(product)
	code = bundle_code(customer_doc.name, product.name, sticker.name)
	if planned_code:
		if planned_code != code or frappe.db.exists("Item", code) or frappe.db.exists("Product Bundle", code):
			return _existing(planned_code, product, sticker, customer_doc, profile)
	elif frappe.db.exists("Item", code) or frappe.db.exists("Product Bundle", code):
		return _existing(code, product, sticker, customer_doc, profile)
	else:
		matches = []
		for row in frappe.get_list("Product Bundle", filters=[
			["Product Bundle", "disabled", "=", 0], ["Product Bundle Item", "item_code", "=", sticker.name],
		], fields=["name", "new_item_code"], limit_page_length=0):
			try:
				bundle, parent = _read("Product Bundle", row["name"]), _read("Item", row["new_item_code"])
			except (frappe.PermissionError, frappe.DoesNotExistError):
				continue
			if _compatible(parent, bundle, product, sticker, profile):
				matches.append((parent, bundle))
		if len(matches) > 1:
			raise BundleInputError("存在多个完全相同的可用组合商品，请先整理重复组合后再开单。")
		if matches:
			return _result(*matches[0], product, sticker, customer_doc, "reuse", profile)
	# Check both create permissions before the first write. Native insert validates
	# linked records too; this helper never uses ignore_permissions or commit.
	parent = frappe.new_doc("Item")
	bundle = frappe.new_doc("Product Bundle")
	parent.check_permission("create")
	bundle.check_permission("create")
	name = f"{product.get('item_name') or product.name}＋{sticker.get('item_name') or sticker.name}"
	if len(name) > 140:
		name = name[:127] + "…" + code[-12:]
	parent.update({**deepcopy(profile), "item_code": code, "item_name": name, "is_stock_item": 0,
		"is_sales_item": 1, "is_purchase_item": 0, "include_item_in_manufacturing": 0,
		"is_fixed_asset": 0, "has_variants": 0, "disabled": 0, "image": product.get("image"),
		"description": (f"<p>客户：{escape(customer_doc.get('customer_name') or customer_doc.name)}</p>"
			f"<p>每件 {escape(product.get('item_name') or product.name)} 贴一张 "
			f"{escape(sticker.get('item_name') or sticker.name)}。</p>"
			"<p>贴纸售价为 0；出库分别扣减商品和贴纸库存。</p>")})
	parent.insert()
	bundle.update({"new_item_code": parent.name, "description": name, "disabled": 0,
		"items": [{"item_code": item.name, "qty": 1, "uom": item.get("stock_uom"),
			"description": escape(item.get("item_name") or item.name)} for item in (product, sticker)]})
	bundle.insert()
	if not _compatible(parent, bundle, product, sticker, profile):
		raise BundleInputError("原生保存后的组合配置与审核不一致，请管理员核对物料默认设置。")
	return _result(parent, bundle, product, sticker, customer_doc, "create", profile)


def clear_temporary_caches(codes):
	"""Call after preview rollback, including when subsequent native validation fails."""
	for code in dict.fromkeys(codes):
		for doctype in ("Item", "Product Bundle"):
			frappe.clear_document_cache(doctype, code)
