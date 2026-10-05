"""List -> explicit human selection -> one approval -> native Delivery Note draft.

No commit: Flow owns the transaction. No submission, stock movement, Shipment,
carrier booking, payment posting, or business-permission elevation happens here.
"""
from datetime import timedelta
from urllib.parse import quote
import json
import re
import uuid

import frappe
from frappe.utils import nowdate, now_datetime, strip_html

from .delivery_note_rules import DeliveryInputError, available_rows, get_payment_summary, number, select_rows
from .sales_order_flow import _actor, _scope, _hash, _json, _without_price_maintenance

TTL = 1800
VERSION = 2
SERVICE = "Flow Delivery Note"
STAGES = (
    ("order", "核对订单", "核对订单权限、状态、实际收款和剩余出库数量"),
    ("selection", "确认本次商品", "先列清明细，再使用客服明确选择的商品数量或全部出库"),
    ("preview", "生成审核摘要", "通过原生订单转换核对仓库、金额、地址与组合包"),
    ("save", "保存出库草稿", "批准后重新核对并保存原生出库单草稿"),
    ("verify", "回读核验", "核对订单行关联、数量、仓库、金额及草稿状态"),
)


def _steps():
    return [{"key": k, "name": n, "action": a, "status": "not_run", "reason": "前序步骤尚未完成，未执行。"}
            for k, n, a in STAGES]


def _mark(steps, key, status, reason):
    next(s for s in steps if s["key"] == key).update(status=status, reason=reason)


def _failure(exc, steps, stage, rolled_back=False):
    for step in steps:
        if rolled_back and step["status"] == "saved":
            step.update(status="rolled_back", reason="后续核验失败，本次保存已撤回。")
    _mark(steps, stage, "failed", strip_html(str(exc)))
    return {"status": "needs_input" if isinstance(exc, DeliveryInputError) else "error", "verified": False,
            "reason": strip_html(str(exc)), "missing": getattr(exc, "fields", []), "failed_step": stage,
            "rolled_back": rolled_back, "steps": steps, "message": "出库单尚未创建，请按原因处理后继续。"}


def _rollback(point):
    try:
        frappe.db.rollback(save_point=point)
    except Exception as exc:
        # MariaDB discards all savepoints when it already rolled back a deadlock
        # victim. Complete the rollback without replacing the original reason.
        if not exc.args or exc.args[0] != 1305:
            raise
        frappe.db.rollback()


def _key(kind, token):
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise DeliveryInputError("出库方案编号无效，请重新列出订单明细。", [kind + "_token"])
    return "flow_delivery_" + kind + ":" + token


def _last_customer_message(scope):
    if scope == "direct":
        return None
    return frappe.db.get_value("Flow Session Message", {"parent": scope, "parenttype": "Flow Session", "role": "user"},
                               "name", order_by="idx desc")


def _store(kind, values):
    plan = {**values, "user": _actor(), "scope": _scope(), "site": frappe.local.site, "version": VERSION}
    token = _hash(plan)[:32]
    plan["expires_at"] = str(now_datetime() + timedelta(seconds=TTL))
    frappe.cache.set_value(_key(kind, token), plan, expires_in_sec=TTL)
    return token


def _load(kind, token):
    plan = frappe.cache.get_value(_key(kind, token))
    if not plan or plan.get("version") != VERSION or now_datetime() > frappe.utils.get_datetime(plan["expires_at"]):
        raise DeliveryInputError("出库明细或方案已过期，请重新列出明细、选择并审核。", [kind + "_token"])
    if plan.get("user") != _actor() or plan.get("scope") != _scope() or plan.get("site") != frappe.local.site:
        raise frappe.PermissionError("此出库方案不属于当前登录人或当前对话。")
    return plan


def _add_bundle_components(order, rows):
    """Show this order's saved pairing, scaled to the still selectable quantity.

    Packed Item.qty is already in component stock units. Divide by the ordered
    sales quantity, not stock_qty, so a box conversion is applied exactly once.
    """
    sources = {r.name: r for r in order.get("items") or []}
    choices = {r["sales_order_item"]: r for r in rows}
    for row in rows:
        row["components"] = []
    for component in order.get("packed_items") or []:
        parent = component.get("parent_detail_docname")
        if parent not in choices:
            continue
        source, choice = sources[parent], choices[parent]
        if (component.get("parent_item") != source.item_code
                or not component.get("item_code") or number(component.get("qty")) <= 0):
            raise DeliveryInputError("原订单组合商品的组件关联或数量异常，请先核对原订单。", ["sales_order"])
        ratio = number(component.qty) / number(source.qty)
        choice["components"].append({
            "item_code": component.item_code, "item_name": component.get("item_name") or component.item_code,
            "qty_per_unit": float(ratio), "available_qty": float(ratio * number(choice["available_qty"])),
            "uom": component.get("uom"), "warehouse": component.get("warehouse") or source.get("warehouse"),
        })


def _validate_delivery_components(doc, selected):
    """Never silently regenerate a different pairing from an edited bundle."""
    parents = {r.name: r for r in doc.get("items") or []}
    actual = {}
    for component in doc.get("packed_items") or []:
        parent = parents.get(component.get("parent_detail_docname"))
        if not parent or component.get("parent_item") != parent.item_code:
            raise DeliveryInputError("出库组合组件没有准确关联到商品行，未创建出库单。")
        key = (parent.so_detail, component.get("item_code"), component.get("uom"), component.get("warehouse"))
        actual[key] = actual.get(key, number(0)) + number(component.get("qty"))
    expected = {}
    for row in selected:
        for component in row.get("components") or []:
            key = (row["sales_order_item"], component["item_code"], component["uom"], component["warehouse"])
            expected[key] = expected.get(key, number(0)) + number(row["qty"]) * number(component["qty_per_unit"])
    if set(actual) != set(expected) or any(abs(actual[key] - expected[key]) > number("0.000001") for key in expected):
        raise DeliveryInputError("组合商品的巧克粉、贴纸、数量或仓库与原订单不一致，请先核对组合配置和原订单，不能替换配套贴纸后直接出库。", ["sales_order"])


def _order_state(name, for_update=False):
    order = frappe.get_doc("Sales Order", name, for_update=for_update)
    order.check_permission("read")
    if order.docstatus != 1 or order.status in ("Closed", "Cancelled", "On Hold", "Completed"):
        raise DeliveryInputError("订单必须已提交、处于有效状态且仍待出库；草稿、取消、关闭、暂停或已完成订单不能创建出库单。", ["sales_order"])
    if order.get("skip_delivery_note"):
        raise DeliveryInputError("该订单设置了无需出库单，请先核对原订单业务类型。", ["sales_order"])
    if not frappe.has_permission("Delivery Note", "create"):
        raise frappe.PermissionError("当前客服没有创建出库单的权限，请管理员配置出库单权限。")
    drafts = frappe.db.sql("""select i.so_detail,i.qty,d.name from `tabDelivery Note Item` i
        inner join `tabDelivery Note` d on d.name=i.parent
        where i.against_sales_order=%s and d.docstatus=0 and coalesce(d.is_return,0)=0
        and coalesce(d.status,'')<>'Cancelled' and i.qty>0
        order by d.name,i.idx""" + (" for update" if for_update else ""), name, as_dict=True)
    rows = available_rows(order, drafts)
    if not rows:
        raise DeliveryInputError("该订单已经全部出库，或没有适用于普通出库单的商品行。", ["sales_order"])
    _add_bundle_components(order, rows)
    payment = get_payment_summary(order, for_update=for_update)
    draft_links = []
    for docname in dict.fromkeys(d.name for d in drafts):
        if frappe.has_permission("Delivery Note", "read", doc=docname):
            draft_links.append({"name": docname, "url": "/desk/delivery-note/" + quote(docname, safe="")})
    state = {"sales_order": name, "customer": order.customer, "customer_name": order.customer_name,
             "company": order.company, "currency": order.currency, "order_modified": str(order.modified),
             "payment": payment, "items": rows, "drafts": draft_links,
             "draft_count": len({d.name for d in drafts}), "order_status": order.status}
    return order, state


def get_delivery_note_options(sales_order: str = "", customer: str = "", start: int = 0):
    """先展示有效订单的待出库明细与收款情况，不创建单据。

    已知订单号直接传 sales_order。只有客户时传客户编号 customer，多个订单列出候选；
    未指定订单或客户则分页列出当前账号可读订单。返回行号、销售单位、已出库、草稿占用、
    可选剩余数量及 selection_token，必须展示后等待客服明确实物或全部出库，不能默认全选实物。
    未出过的非库存服务行会在选定实物后按剩余数量默认带上，不扣库存。
    """
    steps, stage = _steps(), "order"
    try:
        _actor()
        if not sales_order:
            if type(start) is not int or start < 0:
                raise DeliveryInputError("订单列表起点必须为非负整数。")
            filters = {"docstatus": 1, "status": ["not in", ["Closed", "Cancelled", "On Hold", "Completed"]], "skip_delivery_note": 0}
            if customer:
                doc = frappe.get_doc("Customer", customer)
                doc.check_permission("read")
                filters["customer"] = doc.name
            orders = frappe.get_list("Sales Order", filters=filters,
                fields=["name", "customer", "customer_name", "transaction_date", "currency", "grand_total", "per_delivered"],
                order_by="transaction_date desc, name desc", limit_start=start, limit_page_length=21)
            choices = []
            for order in orders[:20]:
                try:
                    _, state = _order_state(order.name)
                    choices.append({**dict(order), "payment": state["payment"],
                                    "available_lines": sum(r["available_qty"] > 0 for r in state["items"]),
                                    "draft_count": state["draft_count"]})
                except DeliveryInputError as exc:
                    choices.append({**dict(order), "blocked_reason": str(exc)})
            _mark(steps, stage, "passed", "已按当前账号权限查询订单候选，尚未替客服选择订单或商品。")
            return {"status": "needs_order", "orders": choices, "has_more": len(orders) > 20,
                    "next_start": start + 20 if len(orders) > 20 else None, "steps": steps,
                    "message": "请选择本次出库的订单。" if choices else "当前范围没有可选订单，请核对订单是否存在、已提交及当前账号权限。"}
        _, state = _order_state(sales_order)
        if customer and state["customer"] != customer:
            raise DeliveryInputError("指定客户与订单客户不一致，请核对订单号。", ["sales_order"])
        _mark(steps, stage, "passed", "已核对订单状态、有效收款与草稿占用；尚未创建出库单。")
        eligible = state["payment"]["eligible"] and any(r["available_qty"] > 0 for r in state["items"])
        token = _store("selection", {"state": state, "listed_after_message": _last_customer_message(_scope())}) if eligible else None
        _mark(steps, "selection", "waiting", "请逐行展示待出库商品及组合内的巧克粉和贴纸，等待客服选择行号和数量或明确全部出库；选择组合时按原配套比例出库。")
        return {**state, "status": "needs_selection" if eligible else "blocked", "selection_token": token,
                "steps": steps, "message": "请先展示全部明细，再等待客服选择；全部出库仅指本次可选剩余数量，不含其他草稿占用。" if eligible else
                state["payment"]["message"] if not state["payment"]["eligible"] else "剩余商品已被草稿占用，请先处理已有出库单。"}
    except Exception as exc:
        return _failure(exc, steps, stage)


def _document_summary(doc):
    sources = {r.name: r.so_detail for r in doc.items}
    return {"customer": doc.customer, "customer_name": doc.customer_name, "company": doc.company,
            "currency": doc.currency, "posting_date": str(doc.posting_date),
            "shipping_address": doc.shipping_address_name, "shipping_address_display": strip_html(doc.shipping_address or ""),
            "contact_person": doc.contact_person, "conversion_rate": float(doc.conversion_rate or 0),
            "items": [{"sales_order_item": r.so_detail, "sales_order": r.against_sales_order,
                       "item_code": r.item_code, "item_name": r.item_name, "qty": float(r.qty), "uom": r.uom,
                       "stock_qty": float(r.stock_qty), "stock_uom": r.stock_uom, "warehouse": r.warehouse,
                       "rate": float(r.rate), "amount": float(r.amount), "is_free_item": bool(r.is_free_item)} for r in doc.items],
            "packed_items": [{"sales_order_item": sources.get(r.parent_detail_docname), "parent_item": r.parent_item,
                              "item_code": r.item_code, "item_name": r.get("item_name") or r.item_code,
                              "qty": float(r.qty), "uom": r.uom, "warehouse": r.warehouse} for r in doc.get("packed_items") or []],
            "sales_team": [{"sales_person": r.sales_person, "allocated_percentage": float(r.allocated_percentage)} for r in doc.get("sales_team") or []],
            "net_total": float(doc.net_total or 0), "tax_total": float(doc.total_taxes_and_charges or 0),
            "grand_total": float(doc.grand_total or 0), "rounded_total": float(doc.rounded_total or 0),
            "disable_rounded_total": bool(doc.disable_rounded_total), "terms": doc.terms or ""}


@_without_price_maintenance()
def _build_delivery(order, selected, posting_date):
    from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
    from erpnext.stock.doctype.packed_item.packed_item import make_packing_list

    rows = {r["sales_order_item"]: r for r in selected}
    doc = make_delivery_note(order.name, kwargs={"filtered_children": list(rows), "ignore_pricing_rule": True})
    doc.check_permission("create")
    if len(doc.items) != len(rows) or {r.so_detail for r in doc.items} != set(rows):
        raise DeliveryInputError("原生转换后的商品行与选择不一致，请重新列出可出库明细。", ["selection_token"])
    doc.posting_date = posting_date
    for row in doc.items:
        chosen = rows[row.so_detail]
        if not row.name:
            row.name = "flow-preview-" + row.so_detail
        row.qty = chosen["qty"]
        if row.against_sales_order != order.name or row.item_code != chosen["item_code"]:
            raise DeliveryInputError("订单行关联异常，未创建出库单。")
    doc.set_qty_as_per_stock_uom()
    doc.calculate_taxes_and_totals()
    doc.set("packed_items", [])
    make_packing_list(doc)
    doc.run_method("before_validate")
    doc.run_method("validate")
    previous_name = doc.name
    doc.name = "new-flow-delivery-preview"
    doc.set_parent_in_children()
    try:
        doc._validate()
    finally:
        doc.name = previous_name
        doc.set_parent_in_children()
    for row in doc.items:
        chosen = rows[row.so_detail]
        if (number(row.qty) != number(chosen["qty"]) or number(row.rate) != number(chosen["rate"])
                or row.warehouse != chosen["warehouse"] or bool(row.is_free_item) != chosen["is_free_item"]):
            raise DeliveryInputError("原生核验改变了已选择的数量、订单单价、免费标志或仓库，请先核对原订单。")
    _validate_delivery_components(doc, selected)
    return doc


def preview_delivery_note(selection_token: str, items: list[dict] | None = None, all_remaining: bool = False):
    """客服看到明细并明确选择后预检。items 每行 {row_no 或 sales_order_item, qty}。

    只有客服明确说全部出库才传 all_remaining=true；不能省略实物选择、不能给普通商品自动补贴纸。
    未出过的非库存服务行按剩余数量默认加入本次出库，不扣库存；客服已指定该行数量时沿用指定数量。
    组合商品按原订单的组件比例一起出库，必须展示其巧克粉、贴纸及本次数量供审核。
    返回真实 preview_token 和完整摘要，随后调用创建工具批准一次，预检本身不保存。
    """
    steps, stage = _steps(), "order"
    point = "delivery_preview_" + uuid.uuid4().hex
    frappe.db.savepoint(point)
    try:
        plan = _load("selection", selection_token)
        order, state = _order_state(plan["state"]["sales_order"])
        if _hash(state) != _hash(plan["state"]):
            raise DeliveryInputError("收款、订单明细或其他出库草稿已变化，请重新列出明细供客服选择。", ["selection_token"])
        if not state["payment"]["eligible"]:
            raise DeliveryInputError(state["payment"]["message"], ["payment"])
        _mark(steps, stage, "passed", "订单状态、收款及剩余数量与已展示内容一致。")
        stage = "selection"
        if plan["scope"] != "direct" and _last_customer_message(plan["scope"]) == plan["listed_after_message"]:
            raise DeliveryInputError("已列出明细，但客服尚未回复选择。本轮不能代替客服全选或自行决定商品。", ["items"])
        selected = select_rows(state["items"], items, all_remaining)
        _mark(steps, stage, "passed", "已按客服指定的实物行和数量核对；未选择的商品及贴纸不会出库。未出过的服务行按剩余数量默认带上，不扣库存。")
        stage = "preview"
        posting_date = nowdate()
        doc = _build_delivery(order, selected, posting_date)
        summary = _document_summary(doc)
        token = _store("preview", {"selection_token": selection_token, "state": state, "selected": selected,
                                   "posting_date": posting_date, "summary": summary, "all_remaining": all_remaining})
        _mark(steps, stage, "passed", "原生转换及校验通过，请整体审核商品、数量、仓库、收款与地址后批准一次。")
        return {"status": "preview", "preview_token": token, "sales_order": order.name, "payment": state["payment"],
                "summary": summary, "steps": steps, "requires_confirmation": True,
                "message": "预检完成，尚未保存；批准后只创建出库单草稿，不代表已经发货。"}
    except Exception as exc:
        return _failure(exc, steps, stage)
    finally:
        _rollback(point)


def _existing(token, steps, current=False):
    _key("preview", token)
    name = "flow-dn-" + token
    if not frappe.db.sql("select name from `tabIntegration Request` where name=%s" + (" for update" if current else ""), name):
        return None
    ledger = frappe.get_doc("Integration Request", name, for_update=True)
    data = json.loads(ledger.data or "{}")
    if ledger.integration_request_service != SERVICE or data.get("user") != _actor() or data.get("scope") != _scope():
        raise frappe.PermissionError("此出库请求不属于当前登录人或当前对话。")
    if ledger.status != "Completed" or not ledger.reference_docname:
        raise DeliveryInputError("原出库请求仍在处理中，请先核对原请求。")
    doc = frappe.get_doc("Delivery Note", ledger.reference_docname, for_update=current)
    doc.check_permission("read")
    if doc.docstatus == 2:
        raise DeliveryInputError("此方案对应的出库单已取消，不能重用旧批准；请重新列出明细并选择。")
    for step in steps:
        step.update(status="reused", reason="已返回本次方案对应的原出库单，未重复创建。")
    return {"status": "existing", "verified": True, "delivery_note": doc.name, "docstatus": doc.docstatus,
            "url": "/desk/delivery-note/" + quote(doc.name, safe=""), "summary": _document_summary(doc),
            "steps": steps, "message": "本次方案已有出库单，已返回原单及其当前状态。"}


def _create_delivery_note_draft(preview_token: str):
    """仅用真实预检令牌，经一次原生批准，重新核对后保存出库单草稿。"""
    steps, stage = _steps(), "order"
    point = "delivery_create_" + uuid.uuid4().hex
    frappe.db.savepoint(point)
    try:
        _actor()
        existing = _existing(preview_token, steps)
        if existing:
            return existing
        plan = _load("preview", preview_token)
        # Current locking reads prevent two tools from reserving the same remainder.
        order, state = _order_state(plan["state"]["sales_order"], for_update=True)
        existing = _existing(preview_token, steps, current=True)
        if existing:
            return existing
        if _hash(state) != _hash(plan["state"]):
            raise DeliveryInputError("审批期间收款、订单或其他出库草稿已变化，不能沿用旧批准；请重新列出明细并选择。", ["selection_token"])
        if not state["payment"]["eligible"]:
            raise DeliveryInputError(state["payment"]["message"], ["payment"])
        _mark(steps, stage, "passed", "已锁定原订单，重新核对收款和全部草稿占用。")
        stage = "selection"
        selected = select_rows(state["items"], [{"sales_order_item": r["sales_order_item"], "qty": r["qty"]} for r in plan["selected"]])
        _mark(steps, stage, "passed", "所选商品、数量和订单行仍有效，未默认添加其他商品。")
        stage = "preview"
        doc = _build_delivery(order, selected, plan["posting_date"])
        summary = _document_summary(doc)
        if _hash(summary) != _hash(plan["summary"]):
            raise DeliveryInputError("原生出库内容、仓库或地址已变化，请重新预检并审核。", ["preview_token"])
        _mark(steps, stage, "passed", "当前完整摘要与客服批准的方案一致。")
        stage = "save"
        ledger = frappe.get_doc({"doctype": "Integration Request", "integration_request_service": SERVICE,
            "status": "Queued", "request_id": preview_token, "data": _json({"user": plan["user"], "scope": plan["scope"]}),
            "request_description": "Flow 明确选品并批准一次创建出库单草稿"})
        ledger.flags._name = "flow-dn-" + preview_token
        ledger.insert(ignore_permissions=True)
        with _without_price_maintenance():
            doc.insert()
        _mark(steps, stage, "saved", "原生出库单草稿已保存，正在回读核验。")
        stage = "verify"
        saved = frappe.get_doc("Delivery Note", doc.name)
        saved.check_permission("read")
        if saved.docstatus != 0 or saved.owner != plan["user"] or _hash(_document_summary(saved)) != _hash(summary):
            raise DeliveryInputError("保存后的内容或状态与批准方案不一致，本次保存已撤回。")
        _mark(steps, stage, "completed", "订单行、产品数量、仓库、金额及草稿状态已回读核验；尚未扣库存或实际发货。")
        ledger.status = "Completed"
        ledger.reference_doctype, ledger.reference_docname = "Delivery Note", saved.name
        ledger.output = _json({"delivery_note": saved.name, "verified": True})
        ledger.save(ignore_permissions=True)
        return {"status": "created", "verified": True, "delivery_note": saved.name, "docstatus": 0,
                "url": "/desk/delivery-note/" + quote(saved.name, safe=""), "summary": summary,
                "payment": state["payment"], "steps": steps, "message": "出库单草稿已创建，尚未提交、扣库存或实际发货。"}
    except Exception as exc:
        _rollback(point)
        return _failure(exc, steps, stage, rolled_back=True)


def _confirmation_prompt(args):
    try:
        plan = _load("preview", args.get("preview_token"))
        payment, summary = plan["state"]["payment"], plan["summary"]
        lines = ["🔵 请整体审核：创建出库单草稿", "", "订单：" + plan["state"]["sales_order"],
                 "客户：" + summary["customer_name"], "", "【收款情况】",
                 f"订单金额 {payment['currency']} {payment['order_total']:.2f}；已收 {payment['paid']:.2f}；未收 {payment['remaining']:.2f}"]
        lines.append(("🟠 " if payment["status"] == "partial" else "") + payment["message"])
        lines.extend(["", "【本次明确选择的出库明细】"])
        for row in summary["items"]:
            original = next(r for r in plan["selected"] if r["sales_order_item"] == row["sales_order_item"])
            unit = "件" if row["uom"] == "Nos" else row["uom"]
            if original.get("service"):
                lines.append(f"• 第{original['row_no']}行 {row['item_name']}：{row['qty']:g} {unit}；服务，不扣库存" + ("；免费" if row["is_free_item"] else ""))
            else:
                lines.append(f"• 第{original['row_no']}行 {row['item_name']}：{row['qty']:g} {unit}；仓库：{row['warehouse']}" + ("；免费" if row["is_free_item"] else ""))
            components = [r for r in summary["packed_items"] if r["sales_order_item"] == row["sales_order_item"]]
            if components:
                lines.append("  内含：" + "；".join(
                    f"{r.get('item_name') or r['item_code']} × {r['qty']:g} {r['uom']}（{r['warehouse']}）" for r in components))
        lines.extend(["", "收货地址：" + (summary["shipping_address_display"] or "尚未设置，请核对"),
                      "制单日期：" + summary["posting_date"],
                      f"本次出库单金额：{summary['currency']} {summary['grand_total']:.2f}"])
        lines.extend(["", "请核对本次商品、数量、仓库、地址和收款余额，正确后批准一次；需修改请拒绝并说明。",
                      "只保存出库单草稿，尚未扣库存或实际发货；提交出库及创建物流运单属于后续操作。"])
        return "\n".join(lines)
    except Exception as exc:
        return "当前出库方案不可执行：" + strip_html(str(exc))


from flow.lib.tool import tool

create_delivery_note_draft = tool(_create_delivery_note_draft, name="create_delivery_note_draft",
                                  requires_confirmation=True, confirm_prompt=_confirmation_prompt)
