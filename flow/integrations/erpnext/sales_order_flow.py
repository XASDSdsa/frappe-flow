"""Permission-preserving, reviewed Sales Order drafts for Flow.

Preview uses native pricing inside a rolled-back savepoint. Creation uses the
same native document, a user/session-bound preview, and a transactional unique
Integration Request key; the surrounding Flow request owns the commit.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import uuid
from copy import deepcopy
from contextlib import contextmanager
from datetime import timedelta
from urllib.parse import quote

import frappe
from frappe.utils import nowdate, now_datetime, strip_html

from .sales_order_resolution import STANDALONE_STICKER_WARNING, resolve_order_inputs

SERVICE = "Flow Sales Order"
TTL = 1800
VERSION = 4
DEFAULT_WAREHOUSE = "大坪仓库 - LEYA"
STAGES = (
    ("inputs", "客户、商品与本人销售员", "核对客户、商品数量、贴纸归属和当前登录客服的销售员关联"),
    ("pricing", "价格与交货计划", "按原生规则取得价格、汇率、仓库、地址和税费，计算交货截止日期"),
    ("approval", "订单方案批准", "使用已核对的订单方案，保留一次系统批准"),
    ("save", "保存订单草稿", "通过原生销售订单创建草稿，并登记本次请求防止重复开单"),
    ("verify", "回读订单核验", "核对保存后的客户、商品、贴纸数量金额、销售团队和交货日期"),
)


class OrderInputError(Exception):
    def __init__(self, message, fields=None):
        super().__init__(message)
        self.fields = fields or []


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _steps():
    return [{"key": key, "name": name, "action": action, "status": "not_run", "reason": "前序步骤尚未完成，未执行。"}
            for key, name, action in STAGES]


def _mark(steps, key, status, reason):
    next(row for row in steps if row["key"] == key).update(status=status, reason=reason)


def _actor():
    user = frappe.session.user
    if not user or user == "Guest" or not frappe.db.get_value("User", user, "enabled"):
        raise frappe.PermissionError("请使用已启用的本人账号登录。")
    return user


def _scope():
    run_name = frappe.flags.get("flow_run")
    if not run_name:
        return "direct"
    run = frappe.db.get_value("Flow Run", run_name, ["owner", "session"], as_dict=True)
    if not run or run.owner != frappe.session.user:
        raise frappe.PermissionError("无法核实当前对话的实际操作人，请重新登录后继续。")
    return run.session


def _cache_key(token):
    return "flow_sales_order_preview:" + token


def _ledger_name(token):
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise OrderInputError("订单方案编号无效，请重新预检。", ["preview_token"])
    return "flow-so-" + token


def _read(doctype, name):
    doc = frappe.get_doc(doctype, name)
    doc.check_permission("read")
    return doc


def query_sales_order_details(sales_order: str = "", customer: str = ""):
	"""只读返回一张销售订单的完整商品行和收款摘要。

	传入精确销售订单号时只读取该订单；未传订单号时按当前账号权限选择最新
	一张未取消订单。该工具不读取出库单或发票，也不执行任意代码，专门处理
	“查看最新订单／查看订单明细”这类查询。
	"""
	try:
		def number(value):
			try:
				return float(value or 0)
			except (TypeError, ValueError, OverflowError):
				return 0.0

		if not isinstance(sales_order, str) or not isinstance(customer, str):
			return {"status": "needs_input", "verified": False, "message": "销售订单号和客户编号必须是文本。"}
		sales_order = sales_order.strip()
		customer = customer.strip()
		if sales_order:
			order = _read("Sales Order", sales_order)
		else:
			filters = {"docstatus": ["!=", 2]}
			if customer:
				filters["customer"] = customer
			candidates = frappe.get_list(
				"Sales Order",
				filters=filters,
				fields=[
					"name", "customer", "customer_name", "company", "currency",
					"transaction_date", "delivery_date", "status", "docstatus",
					"grand_total", "advance_paid",
				],
				order_by="creation desc, name desc",
				limit_page_length=1,
			)
			if not candidates:
				return {
					"status": "not_found", "verified": True,
					"message": "当前账号没有可读取的有效销售订单。", "items": [],
				}
			order = _read("Sales Order", candidates[0]["name"])

		items = []
		for row in order.get("items") or []:
			items.append({
				"row_no": row.idx,
				"sales_order_item": row.name,
				"item_code": row.item_code,
				"item_name": row.item_name,
				"description": row.get("description") or "",
				"qty": number(row.qty),
				"uom": row.uom,
				"rate": number(row.rate),
				"amount": number(row.amount),
				"warehouse": row.get("warehouse") or "",
				"delivery_date": str(row.get("delivery_date") or ""),
				"delivered_qty": number(row.get("delivered_qty") or 0),
				"billed_amt": number(row.get("billed_amt") or 0),
				"is_free_item": bool(row.get("is_free_item")),
			})
		return {
			"status": "found",
			"verified": True,
			"sales_order": order.name,
			"customer": order.customer,
			"customer_name": order.get("customer_name") or order.customer,
			"company": order.company,
			"currency": order.currency,
			"transaction_date": str(order.transaction_date or ""),
			"delivery_date": str(order.get("delivery_date") or ""),
			"status_display": order.status,
			"docstatus": order.docstatus,
			"grand_total": number(order.grand_total),
			"advance_paid": number(order.get("advance_paid") or 0),
			"items": items,
			"message": f"已读取销售订单 {order.name} 的完整商品明细和收款摘要。",
		}
	except Exception as exc:
		return {
			"status": "error", "verified": False,
			"message": strip_html(str(exc)) or "读取销售订单失败，请检查权限和订单编号。",
			"items": [],
		}


def _native_rows(resolved):
    allowed = {"item_code", "qty", "uom", "conversion_factor", "delivery_date", "rate", "is_free_item"}
    return [{key: value for key, value in row.items() if key in allowed} for row in resolved["items"]]


def _company(explicit):
    company = explicit or frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
    if not company:
        names = frappe.get_list("Company", pluck="name", limit_page_length=2)
        if len(names) == 1:
            company = names[0]
    if not company:
        raise OrderInputError("系统没有唯一可用的默认公司，请明确本次开单公司。", ["company"])
    _read("Company", company)
    return company


@contextmanager
def _without_price_maintenance():
    """Disable price-master writes only in this request's decoded cached doc.

    No shared cache or database settings are changed; other requests have their
    own object, and every original setting is restored even on validation errors.
    """
    settings = frappe.get_cached_doc("Stock Settings")
    values = {key: settings.get(key) for key in ("auto_insert_price_list_rate_if_missing", "update_existing_price_list_rate")}
    try:
        settings.update({key: 0 for key in values})
        yield
    finally:
        settings.update(values)


@_without_price_maintenance()
def _build_order(resolved, request):
    """All callers must provide a rollback boundary: native pricing can write prices."""
    doc = frappe.new_doc("Sales Order")
    customer = resolved["customer"]
    doc.update({"customer": customer["name"], "company": _company(resolved.get("company")),
                "transaction_date": request["base_date"], "delivery_date": resolved["delivery_date"],
                "order_type": "Sales", "status": "Draft", "docstatus": 0})
    doc.set("sales_team", resolved["sales_team"])
    from erpnext.accounts.party import get_default_price_list
    price_list = request.get("price_list") or get_default_price_list(frappe._dict(customer)) or frappe.db.get_single_value("Selling Settings", "selling_price_list")
    if not price_list:
        raise OrderInputError("客户及系统尚未配置销售价目表，请选择已配置的价目表。", ["price_list"])
    price_doc = _read("Price List", price_list)
    if not price_doc.enabled or not price_doc.selling:
        raise OrderInputError("所选价目表未启用销售用途，请选择启用的销售价目表。", ["price_list"])
    doc.selling_price_list = price_list
    doc.currency = resolved.get("currency") or customer.get("default_currency") or price_doc.currency
    if not doc.currency:
        raise OrderInputError("无法确定客户结算币种，请补充本单实际币种。", ["currency"])
    doc.check_permission("create")
    warehouse = request.get("warehouse") or DEFAULT_WAREHOUSE
    if warehouse:
        wh = _read("Warehouse", warehouse)
        if wh.company != doc.company or wh.is_group or wh.disabled:
            raise OrderInputError("发货仓库必须是本公司启用的实际仓库。", ["warehouse"])
    for values in _native_rows(resolved):
        row = doc.append("items", values)
        if warehouse:
            row.warehouse = warehouse
        if values.get("is_free_item"):
            row.rate = row.price_list_rate = 0
            row.discount_percentage = row.discount_amount = row.margin_rate_or_amount = 0
            row.pricing_rules = None
        elif "rate" not in values:
            row.rate = row.price_list_rate = None
    doc.set_missing_values()
    doc.calculate_taxes_and_totals()
    missing_warehouse_rows = [row for row in doc.items if not row.warehouse and not row.delivered_by_supplier
                              and frappe.get_cached_value("Item", row.item_code, "is_stock_item")]
    if missing_warehouse_rows:
        default_wh = frappe.db.get_single_value("Stock Settings", "default_warehouse")
        candidates = frappe.get_list("Warehouse", filters={"company": doc.company, "disabled": 0, "is_group": 0},
                                     pluck="name", limit_page_length=0)
        fallback = default_wh if default_wh in candidates else candidates[0] if len(candidates) == 1 else None
        if fallback:
            _read("Warehouse", fallback)
            for row in missing_warehouse_rows:
                row.warehouse = fallback
    missing_prices, missing_warehouses = [], []
    for index, row in enumerate(doc.items):
        source = resolved["items"][index] if index < len(resolved["items"]) else {}
        if source.get("is_free_item"):
            row.is_free_item = 1
            row.rate = row.price_list_rate = 0
            row.discount_percentage = row.discount_amount = row.margin_rate_or_amount = 0
            row.pricing_rules = None
        elif float(row.rate or 0) <= 0 and not row.is_free_item:
            missing_prices.append(row.item_name or row.item_code)
        if frappe.get_cached_value("Item", row.item_code, "is_stock_item") and not row.warehouse and not row.delivered_by_supplier:
            missing_warehouses.append(row.item_name or row.item_code)
    if missing_prices or missing_warehouses:
        messages = []
        if missing_prices:
            messages.append("以下收费物料没有有效正数售价：" + "、".join(missing_prices) + "；请补充本单单价或维护有效售价。")
        if missing_warehouses:
            messages.append("以下物料没有可用默认发货仓库：" + "、".join(missing_warehouses) + "；请明确发货仓库。")
        raise OrderInputError(" ".join(messages), (["rates"] if missing_prices else []) + (["warehouse"] if missing_warehouses else []))
    # Party defaults must never replace the trusted current operator allocation.
    doc.set("sales_team", resolved["sales_team"])
    deadline = resolved["delivery_deadline"]
    doc.terms = (doc.terms or "") + "<p>约定交货截止：" + html.escape(deadline) + "（系统业务时区）。</p>"
    if request.get("remarks"):
        doc.terms += "<p>本单说明：" + html.escape(request["remarks"]) + "</p>"
    doc.run_method("before_validate")
    doc.run_method("validate")
    # No official naming-series allocation during preview. Validate required
    # child-parent links using a temporary in-memory parent identifier only.
    previous_name = doc.name
    doc.name = "new-flow-sales-order-preview"
    doc.set_parent_in_children()
    try:
        doc._validate()
    finally:
        doc.name = previous_name
        doc.set_parent_in_children()
    _verify_free_rows(doc, resolved)
    return doc


def _verify_free_rows(doc, resolved):
    for index, source in enumerate(resolved["items"]):
        if source.get("is_free_item"):
            row = doc.items[index]
            if row.item_code != source["item_code"] or float(row.rate or 0) != 0 or float(row.amount or 0) != 0:
                raise OrderInputError("免费贴纸被原生定价改变，本次未保存，请管理员核对定价规则。")


@_without_price_maintenance()
def _build_reviewed_order(resolved, request, *, planned_bundles=None, touched=None):
    """Price actual goods first, then preserve that quotation in native bundles.

    Callers own the savepoint. Preview-only Item/Product Bundle writes are rolled
    back; approval creates the same definitions and order in one transaction.
    """
    doc = _build_order(resolved, request)
    pairs = {row["product_row"]: row for row in resolved["sticker_rows"] if row.get("bundle_eligible")}
    if not pairs:
        return doc, resolved, []
    from .sales_order_bundles import ensure_bundle, bundle_code

    original_totals = tuple(float(doc.get(key) or 0) for key in
                            ("total", "net_total", "total_taxes_and_charges", "grand_total", "rounded_total"))
    sources, rows, bundles, expected = [], [], [], []
    planned = {row["product_row"]: row for row in planned_bundles or []}
    for source, row in zip(resolved["items"], doc.items, strict=True):
        if source.get("row_type") == "sticker" and source.get("bundle_eligible"):
            continue
        source = deepcopy(source)
        sticker = pairs.get(source.get("product_row")) if source.get("row_type") == "product" else None
        if sticker:
            planned_code = planned.get(source["product_row"], {}).get("item_code")
            if touched is not None:
                touched.add(planned_code or bundle_code(resolved["customer"]["name"], source["item_code"], sticker["item_code"]))
            bundle = ensure_bundle(source["item_code"], sticker["item_code"], resolved["customer"]["name"],
                                   planned_code=planned_code)
            if touched is not None:
                touched.add(bundle["item_code"])
            reviewed = planned.get(source["product_row"])
            if reviewed and bundle["signature"] != reviewed["signature"]:
                raise OrderInputError("组合配置已变化，请重新预检并审核。", ["preview_token"])
            bundle["product_row"] = source["product_row"]
            bundles.append(bundle)
            components = [dict(component, total_qty=float(row.stock_qty) * float(component["qty"]))
                          for component in bundle["components"]]
            source.update(row_type="bundle", product_item_code=source["item_code"],
                          product_item_name=source["item_name"], item_code=bundle["item_code"],
                          item_name=bundle["item_name"], sticker_item_code=sticker["item_code"],
                          sticker_item_name=sticker["item_name"], sticker_model=sticker.get("sticker_model"),
                          sticker_version=sticker.get("sticker_version"), components=components)
            row.item_code, row.item_name = bundle["item_code"], bundle["item_name"]
            row.description = html.escape(bundle["item_name"])
            # The native quotation was calculated against the physical chalk,
            # including customer Item Price and discounts. Do not reprice the
            # new parent through unrelated bundle pricing rules on insertion.
            row.pricing_rules = None
        sources.append(source)
        rows.append(row)
        expected.append((row.item_code, float(row.rate or 0), float(row.amount or 0)))
    effective = {**resolved, "items": sources}
    doc.set("items", rows)
    for index, row in enumerate(doc.items, 1):
        row.idx = index
        if not row.name:
            row.name = "flow-bundle-preview-" + uuid.uuid4().hex
    doc.ignore_pricing_rule = 1
    doc.run_method("before_validate")
    doc.run_method("validate")
    if expected != [(row.item_code, float(row.rate or 0), float(row.amount or 0)) for row in doc.items]:
        raise OrderInputError("组合商品的原生定价改变了已核对的巧克粉售价，请管理员检查组合定价设置；本次未保存。")
    if original_totals != tuple(float(doc.get(key) or 0) for key in
                               ("total", "net_total", "total_taxes_and_charges", "grand_total", "rounded_total")):
        raise OrderInputError("组合后的金额或税费与原商品报价不一致，本次未保存，请核对定价及税费设置。")
    _verify_free_rows(doc, effective)
    _verify_bundle_components(doc, effective)
    return doc, effective, bundles


def _verify_bundle_components(doc, resolved):
    parents = {row.name: row for row in doc.items}
    for packed in doc.get("packed_items") or []:
        parent = parents.get(packed.parent_detail_docname)
        if not parent or packed.parent_item != parent.item_code:
            raise OrderInputError("组合组件的商品行关联不一致，本次未保存，请重新预检。")
    for source, parent in zip(resolved["items"], doc.items, strict=True):
        if source.get("row_type") != "bundle":
            continue
        expected = sorted((r["item_code"], r["uom"], round(float(r["total_qty"]), 6))
                          for r in source["components"])
        packed = [r for r in doc.get("packed_items") or [] if r.parent_detail_docname == parent.name]
        actual = sorted((r.item_code, r.uom, round(float(r.qty), 6)) for r in packed)
        if actual != expected or any(r.parent_item != parent.item_code or r.warehouse != parent.warehouse for r in packed):
            raise OrderInputError("组合商品的巧克粉、贴纸数量或仓库不一致，本次未保存，请重新预检。")


def _clear_bundle_caches(codes):
    if codes:
        from .sales_order_bundles import clear_temporary_caches
        clear_temporary_caches(codes)


def _summary_source(row, index, resolved_rows, *, existing=False, row_sources=None):
    if not existing:
        source = resolved_rows[index] if index < len(resolved_rows) else {}
        return source if source.get("item_code") == row.item_code else {}
    if row_sources is not None:
        source = row_sources.get(row.name, {})
        return source if source.get("item_code") == row.item_code else {}
    # Older requests have no saved child-row IDs. Never assign old positional
    # metadata to an order that a user may have reordered or edited manually.
    candidates = [source for source in resolved_rows if source.get("item_code") == row.item_code]
    identities = {(source.get("row_type"), source.get("product_item_code")) for source in candidates}
    return candidates[0] if len(identities) == 1 else {}


def _summary(doc, resolved=None, *, existing=False, row_sources=None):
    resolved_rows = (resolved or {}).get("items", [])
    rows = []
    for index, row in enumerate(doc.items):
        source = _summary_source(row, index, resolved_rows, existing=existing, row_sources=row_sources)
        rows.append({"item_code": row.item_code, "item_name": row.item_name,
                     "qty": float(row.qty), "uom": row.uom, "stock_qty": float(row.stock_qty),
                     "stock_uom": row.stock_uom, "rate": float(row.rate or 0), "amount": float(row.amount or 0),
                     "warehouse": row.warehouse, "delivery_date": str(row.delivery_date),
                     "is_free_item": bool(row.is_free_item), "row_type": source.get("row_type", "native"),
                     "product_item_code": source.get("product_item_code"),
                     "sticker_model": source.get("sticker_model"), "sticker_version": source.get("sticker_version"),
                     "sticker_item_code": source.get("sticker_item_code"),
                     "sticker_item_name": source.get("sticker_item_name"),
                     "product_item_name": source.get("product_item_name"),
                     "components": source.get("components", [])})
    standalone = [row for row in rows if row["row_type"] == "standalone_sticker"]
    warnings = [{"code": "standalone_customer_stickers", "severity": "warning",
                 "item_codes": list(dict.fromkeys(row["item_code"] for row in standalone)),
                 "message": STANDALONE_STICKER_WARNING}] if standalone else []
    return {"customer": doc.customer, "customer_name": doc.customer_name, "company": doc.company,
            "currency": doc.currency, "price_list": doc.selling_price_list,
            "conversion_rate": float(doc.conversion_rate or 0), "price_list_currency": doc.price_list_currency,
            "plc_conversion_rate": float(doc.plc_conversion_rate or 0),
            "transaction_date": str(doc.transaction_date), "delivery_date": str(doc.delivery_date),
            "delivery_deadline": str(doc.delivery_date) + " 23:59:00", "items": rows, "warnings": warnings,
            "timezone": frappe.utils.get_system_timezone(),
            "sales_team": [{"sales_person": row.sales_person, "allocated_percentage": float(row.allocated_percentage)} for row in doc.sales_team],
            "product_qty": sum(r["stock_qty"] for r in rows if r["row_type"] in {"product", "bundle"}),
            "sticker_qty": sum(r["stock_qty"] for r in rows if r["row_type"] in {"sticker", "standalone_sticker", "bundle"}),
            "accompanying_sticker_qty": sum(r["stock_qty"] for r in rows if r["row_type"] in {"sticker", "bundle"}),
            "standalone_sticker_qty": sum(r["stock_qty"] for r in standalone),
            "product_amount": sum(r["amount"] for r in rows if r["row_type"] in {"product", "bundle"}),
            "sticker_amount": sum(r["amount"] for r in rows if r["row_type"] in {"sticker", "standalone_sticker"}),
            "accompanying_sticker_amount": sum(r["amount"] for r in rows if r["row_type"] == "sticker"),
            "standalone_sticker_amount": sum(r["amount"] for r in standalone),
            "total": float(doc.total or 0), "net_total": float(doc.net_total or 0),
            "taxes": [{"charge_type": r.charge_type, "account_head": r.account_head, "rate": float(r.rate or 0), "tax_amount": float(r.tax_amount or 0)} for r in doc.taxes],
            "tax_total": float(doc.total_taxes_and_charges or 0), "grand_total": float(doc.grand_total or 0),
            "rounded_total": float(doc.rounded_total or 0), "disable_rounded_total": bool(doc.disable_rounded_total),
            "customer_address": doc.customer_address, "shipping_address": doc.shipping_address_name,
            "shipping_address_display": strip_html(doc.shipping_address or ""), "contact_person": doc.contact_person,
            "payment_terms_template": doc.payment_terms_template, "terms": doc.terms or ""}


def _resolve(request):
    return resolve_order_inputs(**{k: request.get(k) for k in (
        "customer", "items", "company", "currency", "delivery_date", "delivery_days", "stickers_free", "sticker_mappings", "include_stickers", "base_date", "bundle_mappings")})


def _first_order_sticker_review(summary):
    """Read-only advice, separate from the order fingerprint and monetary review.

    Drafts count as existing orders; cancelled orders do not. Permission-filtered
    history only establishes what this operator can see, not global first-order status.
    """
    try:
        orders = frappe.get_list("Sales Order", filters={"customer": summary["customer"], "docstatus": ["!=", 2]},
                                 fields=["name"], limit_page_length=1)
    except frappe.PermissionError:
        basis = "无权读取历史订单，无法确认是否首单。"
    else:
        if orders:
            return None
        basis = "当前账号未查到该客户未取消的历史订单，按首单提醒。"
    selected = any(row["row_type"] in {"bundle", "sticker", "standalone_sticker"} for row in summary["items"])
    choice = ("本单已选择贴纸：请核对对应商品、贴纸型号／版本和数量。" if selected else
              "本单未列贴纸：请在整单审核时确认是否需要客户贴纸。")
    return {"title": "首单贴纸提醒", "display_mode": "review_notice", "requires_input": False,
            "message": basis + "\n" + choice +
            "\n贴纸售价固定为0元；定制服务费只有明确约定收费时才单独列出。此提醒随整单审核，不增加第二次确认，不自动添加贴纸、组合产品或收费项目。"}


def _error(exc, steps, stage, *, rolled_back=False):
    message = strip_html(str(exc)) or "处理失败，请管理员核对系统错误日志。"
    _mark(steps, stage, "failed", message)
    if rolled_back:
        for row in steps:
            if row["key"] in ("save", "verify") and row["status"] in ("saved", "completed"):
                row.update(status="rolled_back", reason="后续核验失败，本次写入已整体撤回。")
    return {"status": "needs_input" if isinstance(exc, OrderInputError) else "error", "verified": False,
            "message": message, "missing": getattr(exc, "fields", []), "rolled_back": rolled_back,
            "steps": steps}


def preview_sales_order(customer: str, items: list[dict], company: str = "", currency: str = "",
                        delivery_date: str = "", delivery_days: int = 7, include_stickers: bool = False,
                        stickers_free: bool = True, sticker_mappings: list[dict] | None = None,
                        warehouse: str = "", price_list: str = "", remarks: str = "", new_order: bool = False,
                        bundle_mappings: list[dict] | None = None):
    """集中预检订单，无需批准。items 用 item_code 或完整 item_name、qty，可选 uom。

    用户要求配客户贴纸时 include_stickers=true；按实际物理件数一件一张。
    多版本通过 sticker_mappings 指明 product_row（一开始）和贴纸编码或型号版本。
    所有贴纸销售价为0；已由贴纸投入承担的免费定制服务可用非库存服务物料并明确传 is_free_item=true、rate=0，普通库存商品仍必须有正数售价。巧克粉和贴纸同在 items 时自动检查搭配；AI可用
    bundle_mappings=[{product_row:1,sticker_row:2}]明确同单搭配，均为原items行号。
    独立/备用贴纸行传standalone=true。组合需一颗巧克粉一张贴纸，歧义集中询问。
    没提到贴纸不自动添加。预检不保留物料或单据写入，批准一次后创建/复用原生产品组合。
    当前账号未查到客户未取消历史订单时，整体审核卡片提醒选择贴纸服务；无权核实会明确说明。
    业务顺序：先客户档案，再客户贴纸物料，最后销售订单。交期默认自然日七天后当天23:59。
    不要求提供本人姓名、邮箱或销售员。默认创建草稿。new_order 仅用于用户明确要求另开相同新单。
    """
    steps, stage = _steps(), "inputs"
    touched = set()
    from .reviewed_flow import Boundary
    boundary = Boundary()
    try:
        user, scope = _actor(), _scope()
        if not frappe.has_permission("Sales Order", "create"):
            raise frappe.PermissionError("当前账号没有创建销售订单的权限，请管理员处理销售订单权限。")
        if not isinstance(items, list) or len(items) > 100 or (sticker_mappings and len(sticker_mappings) > 100) or (bundle_mappings and len(bundle_mappings) > 100):
            raise OrderInputError("一次订单最多支持 100 行商品及 100 行贴纸，请分单处理。", ["items"])
        if type(new_order) is not bool or not isinstance(remarks, str) or len(remarks) > 4000:
            raise OrderInputError("订单说明最长 4000 字，另开新单标记必须为布尔值。")
        request = {"customer": customer, "items": items, "company": company or None, "currency": currency or None,
                   "delivery_date": delivery_date or None, "delivery_days": delivery_days, "include_stickers": include_stickers,
                   "stickers_free": stickers_free, "sticker_mappings": sticker_mappings,
                   "bundle_mappings": bundle_mappings,
                   "warehouse": warehouse or None, "price_list": price_list or None,
                   "remarks": remarks, "base_date": nowdate()}
        resolved = _resolve(request)
        if resolved["status"] != "ready":
            _mark(steps, "inputs", "needs_input", "；".join(issue["message"] for issue in resolved["issues"]))
            return {**resolved, "verified": False, "steps": steps}
        _mark(steps, "inputs", "passed", "已核对当前登录人和唯一启用销售员，客户、商品、贴纸归属与数量有效。")
        stage = "pricing"
        doc, resolved, bundles = _build_reviewed_order(resolved, request, touched=touched)
        summary = _summary(doc, resolved)
        _mark(steps, "pricing", "passed", "已按原生规则核对明细价格、汇率和配置；交期为 " + summary["delivery_deadline"] + "。")
        fingerprint = _hash({"version": VERSION, "user": user, "scope": scope, "request": request, "summary": summary})
        if not new_order:
            prior = frappe.db.get_value("Integration Request", {"integration_request_service": SERVICE,
                "request_id": fingerprint, "status": "Completed"}, "name")
            if prior:
                return _existing(prior.removeprefix("flow-so-"), steps)
        # The same reviewed request receives the same key even when two preview
        # workers race. Explicit second-order intent gets a distinct identity.
        token = uuid.uuid4().hex if new_order else fingerprint[:32]
        sticker_service_review = _first_order_sticker_review(summary)
        plan = {"version": VERSION, "user": user, "scope": scope, "site": frappe.local.site,
                "request": request, "resolved": resolved, "summary": summary, "bundles": bundles, "fingerprint": fingerprint,
                "sticker_service_review": sticker_service_review,
                "expires_at": str(now_datetime() + timedelta(seconds=TTL))}
        frappe.cache.set_value(_cache_key(token), plan, expires_in_sec=TTL)
        _mark(steps, "approval", "needs_confirmation", "预检完成，尚未保存；核对摘要后批准一次即可创建草稿。")
        return {"status": "preview", "verified": False, "preview_token": token, "summary": summary,
                "sticker_service_review": sticker_service_review,
                "warnings": summary["warnings"],
                "expires_at": plan["expires_at"], "steps": steps,
                "defaults_used": {"sales_person": resolved["sales_team"][0]["sales_person"], "allocated_percentage": 100,
                                  "delivery_date": summary["delivery_date"], "stickers_free": stickers_free},
                "message": "预检完成，尚未保存。请核对订单摘要后使用本方案批准一次。"}
    except Exception as exc:
        return _error(exc, steps, stage)
    finally:
        # Native pricing may insert/update Item Price. No preview database write survives.
        boundary.rollback()
        _clear_bundle_caches(touched)


def _load_plan(token):
    _ledger_name(token)
    user, scope = _actor(), _scope()
    plan = frappe.cache.get_value(_cache_key(token))
    if not plan:
        raise OrderInputError("订单方案已过期，请重新预检后核对并批准。", ["preview_token"])
    if plan.get("user") != user or plan.get("scope") != scope or plan.get("site") != frappe.local.site:
        raise frappe.PermissionError("该订单方案不属于当前登录人或当前对话，不能使用。")
    if plan.get("version") != VERSION or now_datetime() > frappe.utils.get_datetime(plan["expires_at"]):
        raise OrderInputError("订单方案已过期，请重新预检。", ["preview_token"])
    return plan


def _existing(token, steps):
    name = _ledger_name(token)
    if not frappe.db.exists("Integration Request", name):
        return None
    ledger = frappe.get_doc("Integration Request", name, for_update=True)
    data = json.loads(ledger.data or "{}")
    if ledger.integration_request_service != SERVICE or data.get("user") != _actor() or data.get("scope") != _scope():
        raise frappe.PermissionError("该开单请求不属于当前登录人或当前对话。")
    if ledger.status != "Completed" or not ledger.reference_docname:
        raise OrderInputError("此开单请求仍在处理中，请先核对原请求，勿另开新单。")
    order = _read("Sales Order", ledger.reference_docname)
    if order.docstatus == 2:
        raise OrderInputError("此请求已对应一张取消的订单，不能重复使用；确需另开订单请明确另开新单。")
    for row in steps:
        row.update(status="reused", reason="已找到本次方案对应的原销售订单，未重复创建。")
    return {"status": "existing", "verified": True, "sales_order": order.name,
            "url": "/app/sales-order/" + quote(order.name, safe=""), "docstatus": order.docstatus,
            "summary": _summary(order, data.get("resolved"), existing=True, row_sources=data.get("row_sources")), "steps": steps,
            "message": "本次方案已有订单，已返回原订单，未重复创建。"}


def _create_sales_order_draft(preview_token: str):
    """批准一次保存已预检的销售订单草稿；请直接使用预检返回的 preview_token。"""
    steps, stage = _steps(), "inputs"
    touched = set()
    from .reviewed_flow import Boundary
    boundary = Boundary()
    try:
        _actor()
        existing = _existing(preview_token, steps)
        if existing:
            return existing
        plan = _load_plan(preview_token)
        # Serialize the same token in the database, including concurrent Flow workers.
        ledger = frappe.get_doc({"doctype": "Integration Request", "integration_request_service": SERVICE,
                                 "request_id": plan["fingerprint"], "status": "Queued",
                                 "data": _json({"user": plan["user"], "scope": plan["scope"], "resolved": plan["resolved"]}),
                                 "request_description": "Flow 销售订单草稿，一次批准"})
        ledger.flags._name = _ledger_name(preview_token)
        try:
            ledger.insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            boundary.rollback()
            return _existing(preview_token, steps)
        resolved = _resolve(plan["request"])
        if resolved["status"] != "ready":
            raise OrderInputError("订单资料已变化：" + "；".join(i["message"] for i in resolved["issues"]), resolved["missing"])
        _mark(steps, "inputs", "passed", "已重新确认当前登录人、客户、商品及销售员关联。")
        stage = "pricing"
        # Native price maintenance is disabled locally. Keep bundle definitions
        # inside the same outer transaction as the approved Sales Order.
        doc, resolved, bundles = _build_reviewed_order(resolved, plan["request"],
                                                     planned_bundles=plan.get("bundles"), touched=touched)
        summary = _summary(doc, resolved)
        if _hash(summary) != _hash(plan["summary"]):
            raise OrderInputError("价格、汇率、地址、交货计划或销售员等订单内容已变化，请重新预检并核对新摘要后批准。", ["preview_token"])
        _mark(steps, "pricing", "passed", "复核结果与批准前的摘要一致，收费与免费明细均按已审核金额保留。")
        _mark(steps, "approval", "completed", "已使用当前对话已批准的固定方案。")
        stage = "save"
        with _without_price_maintenance():
            doc.insert()
        _mark(steps, "save", "saved", "原生销售订单草稿已写入，正在回读核验。")
        stage = "verify"
        saved = _read("Sales Order", doc.name)
        if saved.docstatus != 0 or saved.owner != plan["user"]:
            raise OrderInputError("订单状态或创建人核验不一致，本次写入已撤回。")
        _verify_free_rows(saved, resolved)
        _verify_bundle_components(saved, resolved)
        if _hash(_summary(saved, resolved)) != _hash(summary):
            raise OrderInputError("保存后的订单明细、价格、交期或销售团队与已批准方案不一致，本次写入已撤回。")
        _mark(steps, "verify", "completed", "已回读核对客户、全部商品和贴纸数量金额、本人销售团队100%及交货日期。")
        result = {"status": "created", "verified": True, "sales_order": saved.name, "docstatus": saved.docstatus,
                  "url": "/app/sales-order/" + quote(saved.name, safe=""), "summary": _summary(saved, resolved),
                  "steps": steps, "message": "销售订单草稿已创建并核验完成。"}
        ledger.status = "Completed"
        ledger_data = json.loads(ledger.data or "{}")
        ledger_data["resolved"] = resolved
        ledger_data["bundles"] = bundles
        ledger_data["row_sources"] = {row.name: resolved["items"][index]
                                      for index, row in enumerate(saved.items) if index < len(resolved["items"])}
        ledger.data = _json(ledger_data)
        ledger.reference_doctype, ledger.reference_docname = "Sales Order", saved.name
        ledger.output = _json({"sales_order": saved.name, "verified": True})
        ledger.save(ignore_permissions=True)
        return result
    except Exception as exc:
        boundary.rollback()
        _clear_bundle_caches(touched)
        return _error(exc, steps, stage, rolled_back=True)


def _confirmation_prompt(args):
    """Build the actual approval card from trusted preview contents, not AI prose."""
    try:
        plan = _load_plan(args.get("preview_token"))
        bundle_actions = {b["item_code"]: b["disposition"] for b in plan.get("bundles", [])}
        summary = plan["summary"]
        lines = ["🔵 请整体审核：销售订单草稿（尚未保存）", "", "客户：" + summary["customer_name"]]
        if review := plan.get("sticker_service_review"):
            lines.extend(["", "⚠️ **" + review["title"] + "**", review["message"]])
        for warning in summary.get("warnings", []):
            lines.extend(["", "⚠️ **重点审核：独立销售贴纸**", warning["message"]])
        for kind, heading in (("bundle", "【组合商品：巧克粉＋客户贴纸】"), ("product", "【商品】"), ("sticker", "【配套贴纸】"),
                              ("standalone_sticker", "【独立销售贴纸】"), ("native", "【其他原生明细】")):
            rows = [r for r in summary["items"] if r["row_type"] == kind]
            if not rows:
                continue
            if heading:
                lines.extend(["", heading])
            for row in rows:
                unit = ("张" if kind in {"sticker", "standalone_sticker"} else "颗" if row['item_code'].startswith('SD-') else "件") if row['uom'] == 'Nos' else row['uom']
                price = "免费，金额0.00" if row['is_free_item'] else f"单价 {summary['currency']} {row['rate']:g}，小计 {row['amount']:.2f}"
                detail = ""
                if kind == "bundle":
                    detail = ("；复用已有组合" if bundle_actions.get(row["item_code"]) == "reuse" else "；批准后新建组合") + f"；贴 {row['sticker_item_name']}，{row['stock_qty']:g}张，贴纸0元"
                if kind in {"sticker", "standalone_sticker"}:
                    identity = "/".join(str(row.get(key)) for key in ("sticker_model", "sticker_version") if row.get(key))
                    detail = ("；型号版本 " + identity) if identity else ""
                    if row.get("product_item_code"):
                        detail += "；对应商品 " + row["product_item_code"]
                lines.append(f"• {row['item_name']}：{row['qty']:g}{unit}；{price}{detail}")
        if plan.get("bundles"):
            lines.append("组合按已审核搭配复用或新建；出库分别扣巧克粉与贴纸库存。")
        warehouses = list(dict.fromkeys(r['warehouse'] for r in summary['items'] if r['warehouse']))
        lines.extend(["", "【交货与负责人】", "发货仓库：" + "、".join(warehouses),
                      "销售员：" + summary["sales_team"][0]["sales_person"] + "（默认当前客服，业绩100%）",
                      "交货截止：" + summary['delivery_date'] + " 23:59前（" + summary["timezone"] + "）",
                      "收货地址：" + (summary['shipping_address_display'] or "未设置收货地址，请核对"),
                      "", "【金额】", f"商品：{summary['currency']} {summary['product_amount']:.2f}",
                      f"配套贴纸：{summary['currency']} {summary['accompanying_sticker_amount']:.2f}",
                      f"独立贴纸：{summary['currency']} {summary['standalone_sticker_amount']:.2f}",
                      f"已配置税费：{summary['currency']} {summary['tax_total']:.2f}",
                      f"订单总额：{summary['currency']} {summary['grand_total']:.2f}",
                      "未核定的运费未另行添加。", "", "默认值可修改。请核对完整订单，正确后点批准；需修改请点拒绝并说明。",
                      "批准后保存草稿，提交和发货由后续业务操作处理。"])
        return "\n".join(lines)
    except Exception as exc:
        return "当前订单方案不可执行：" + strip_html(str(exc))


from flow.lib.tool import tool
create_sales_order_draft = tool(_create_sales_order_draft, name="create_sales_order_draft", requires_confirmation=True,
                          confirm_prompt=_confirmation_prompt)
