"""Reviewed Delivery Note -> SF label workflow. Carrier creation runs after commit."""
from copy import deepcopy
from datetime import timedelta
import json
import re
from urllib.parse import quote
import uuid

import frappe
from frappe.utils import now_datetime, get_datetime, strip_html

from erpnext_shipping.sf_international import shipping
from .sales_order_flow import _actor, _scope, _json
from erpnext_shipping.sf_international.reviewed_booking import (
    canonical_hash as _hash, parcels as _parcels, payload_hash as _payload_hash,
    validate_reviewed_booking,
)
from .delivery_note_flow import _last_customer_message, _rollback
from erpnext_shipping.sf_international.sf_label_rules import (LabelInputError, normalize_postcode, postcode_candidates,
                             validate_parcels, parcel_totals, apply_details, validate_customs,
                             validate_sf_address)

SERVICE = "Flow SF Label"
REASON = "Flow 整体批准创建顺丰面单"
TTL = 1800
PAGE_SIZE = 12
STAGES = (("source", "核对出库单"), ("address", "选择收货地区"),
          ("review", "整体审核"), ("booking", "顺丰下单"), ("label", "生成面单 PDF"))


def _steps():
    return [{"key": key, "name": name, "status": "not_run", "reason": "前序步骤尚未完成。"}
            for key, name in STAGES]


def _mark(steps, key, status, reason):
    next(row for row in steps if row["key"] == key).update(status=status, reason=reason)


def _failure(exc, steps, stage):
    reason = strip_html(str(exc))
    _mark(steps, stage, "blocked", reason)
    return {"status": "needs_input" if isinstance(exc, LabelInputError) else "blocked",
            "verified": False, "reason": reason, "missing": getattr(exc, "fields", []),
            "steps": steps, "message": "本步未完成，请按原因处理；已有下单结果以原运单及面单历史为准。"}


def _key(kind, token):
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise LabelInputError("面单方案编号无效，请重新准备面单。")
    return "flow_sf_label_" + kind + ":" + token


def _store(kind, values):
    token = uuid.uuid4().hex
    plan = {**deepcopy(values), "user": _actor(), "scope": _scope(), "site": frappe.local.site,
            "expires_at": str(now_datetime() + timedelta(seconds=TTL))}
    frappe.cache.set_value(_key(kind, token), plan, expires_in_sec=TTL)
    return token


def _load(kind, token):
    plan = frappe.cache.get_value(_key(kind, token))
    if not plan or now_datetime() > get_datetime(plan["expires_at"]):
        raise LabelInputError("面单方案已过期，请重新准备、选择地址并审核。")
    if (plan["user"], plan["scope"], plan["site"]) != (_actor(), _scope(), frappe.local.site):
        raise frappe.PermissionError("此面单方案不属于当前登录人或对话。")
    return plan




def _source(delivery_note, lock=False):
    from erpnext.stock.doctype.shipment.accounting_validation import validate_delivery_accounting

    dn = frappe.get_doc("Delivery Note", delivery_note, for_update=lock)
    dn.check_permission("read")
    if dn.docstatus != 1 or dn.get("is_return") or dn.get("status") in ("Closed", "Cancelled"):
        raise LabelInputError("出库单须已提交且有效，不能是草稿、退货或取消单；本工具不会代为提交出库单。", ["delivery_note"])
    validate_delivery_accounting(dn.name, dn.company)
    links = frappe.db.sql("""select distinct s.name from `tabShipment` s
        inner join `tabShipment Delivery Note` d on d.parent=s.name
        where d.delivery_note=%s and d.parenttype='Shipment' and s.docstatus<2
        order by s.name""" + (" for update" if lock else ""), dn.name, as_dict=True)
    if len(links) > 1:
        raise LabelInputError("该出库单关联多个有效运单，请先在出库单中明确处理已有运单，不能自动选一个或重复创建。")
    doc = frappe.get_doc("Shipment", links[0].name, for_update=lock) if links else None
    if doc:
        doc.check_permission("read")
        if shipping._sf_waybill(doc) or doc.get("sf_waybill_pending_record"):
            if not shipping._is_sf_shipment(doc):
                raise LabelInputError("已有其他物流公司的有效运单，请从原运单核对，不能重复创建顺丰面单。")
            return dn, doc, None
        if doc.docstatus != 0 or doc.get("sf_intercept_status") or doc.get("sf_carrier_cancelled") or shipping._is_cancelled(doc):
            raise LabelInputError("已有运单不是可编辑的待下单草稿，请从原运单处理，不能重新创建面单。")
        if {r.delivery_note for r in doc.get("shipment_delivery_note") or []} != {dn.name}:
            raise LabelInputError("已有运单合并了其他出库单，请在原运单核对完整发货范围。")
        if doc.get("delivery_customer") != dn.customer or doc.get("pickup_company") != dn.company:
            raise LabelInputError("已有运单的客户或公司与出库单不一致，请先核对。")
        doc.check_permission("write")
    else:
        frappe.has_permission("Shipment", "create", throw=True)
        from erpnext.stock.doctype.delivery_note.delivery_note import make_shipment
        doc = make_shipment(dn.name)
        shipping._prepare_mapped_shipment(doc, dn)
    frappe.has_permission("Shipment", "submit", doc=doc, throw=True)
    if not shipping.is_enabled():
        raise LabelInputError("顺丰接口未启用，请管理员检查顺丰设置。")
    settings = shipping.get_settings()
    receiver = shipping._party_from_address(doc.delivery_address_name, doc.delivery_contact_name)
    receiver = shipping._fill_receiver_gaps(receiver, delivery_to=doc.get("delivery_to"),
        delivery_contact_name=doc.get("delivery_contact_name"), delivery_contact=doc.get("delivery_contact"))
    sender = shipping._sender_from_warehouse(doc=doc, pickup_address_name=doc.get("pickup_address_name"))
    products = shipping._product_choices(settings)
    saved_form = shipping._parse_form_json(doc.get("sf_form_json")) or {}
    customs = shipping._customs_presets(settings)
    customs.update({key: saved_form[key] for key in customs if saved_form.get(key) not in (None, "")})
    product = saved_form.get("product_code") or next((p["product_code"] for p in products if p.get("is_preferred")), shipping.DEFAULT_PRODUCT_CODE)
    state = {"delivery_note": dn.name, "dn_modified": str(dn.modified), "customer": dn.customer,
        "customer_name": dn.customer_name, "company": dn.company,
        "shipment": doc.name if links else None, "shipment_modified": str(doc.modified) if links else None,
        "address_name": doc.delivery_address_name, "receiver": receiver, "sender": sender,
        "parcels": _parcels(doc), "customs": customs, "product_code": str(product), "products": products,
        "goods": [{"item_code": r.item_code, "item_name": r.item_name, "qty": r.qty, "uom": r.uom,
                   "warehouse": r.warehouse} for r in dn.items]}
    return dn, doc, state


def _history(state):
    # Existing helpers enforce current country + postcode + full street equality,
    # a real booked identity, readable shipments and no cancelled/candidate labels.
    receiver = shipping._last_sf_receiver_for_address(state["address_name"])
    if receiver:
        return [{**receiver, "source": "history", "badge": "🟢 历史成功下单地址 · 优先推荐 · 仍需客服选择"}]
    receiver = shipping._saved_sf_receiver(state["address_name"])
    if receiver:
        return [{**receiver, "source": "memory", "badge": "🟠 地址记忆 · 未取得可读历史单据证明 · 请核对"}]
    return []


def _page(plan, token, start=0):
    if type(start) is not int or start < 0 or (start and start >= len(plan["choices"])):
        raise LabelInputError("地址列表页码无效。")
    rows = plan["choices"][start:start + PAGE_SIZE]
    # Paging exposes actual candidates; it does not approve any candidate.
    seen = set(plan.get("shown_ids", []))
    messages = plan.setdefault("choice_messages", {})
    for row in rows:
        if row["choice_id"] not in seen:
            messages[row["choice_id"]] = _last_customer_message(plan["scope"])
    plan["shown_ids"] = sorted(seen | {r["choice_id"] for r in rows})
    remaining = max(1, int((get_datetime(plan["expires_at"]) - now_datetime()).total_seconds()))
    frappe.cache.set_value(_key("preparation", token), plan, expires_in_sec=remaining)
    return {"preparation_token": token, "choices": rows, "total_choices": len(plan["choices"]),
            "next_start": start + PAGE_SIZE if start + PAGE_SIZE < len(plan["choices"]) else None}


def prepare_sf_label(delivery_note: str, lookup_postcode: bool = False):
    """核对已提交出库单，优先列历史地址；没有历史或要求重新查询时邮编反查。

    必须展示候选编号并等客服选择，即使只有一个历史地址。AI 只能从实际候选中推荐，
    不能自行编造省市。同步展示包裹、产品和申报预设，缺失信息一次补齐。
    """
    steps, stage = _steps(), "source"
    try:
        _actor()
        _, doc, state = _source(delivery_note)
        if state is None:
            return get_sf_label_result(doc.name)
        _mark(steps, stage, "passed", "出库单已提交，客户、商品及当前账号权限已核对；未创建面单。")
        stage = "address"
        receiver = state["receiver"]
        if not receiver.get("country") or not receiver.get("post_code") or not receiver.get("address"):
            raise LabelInputError("请先补齐出库单收货地址的国家、邮编及街道，然后重新准备面单。", ["address"])
        choices = _history(state)
        lookup_error = None
        if not choices or lookup_postcode:
            try:
                rows = shipping.lookup_sf_postcode(receiver["country"], receiver["post_code"])
                api = postcode_candidates(rows, receiver)
                postal = normalize_postcode(receiver["post_code"])
                if not api and receiver["country"] == "US" and re.fullmatch(r"[0-9]{9}", postal):
                    api = postcode_candidates(shipping.lookup_sf_postcode("US", postal[:5]), receiver)
                choices += [{**r, "source": "postcode", "badge": "顺丰邮编反查候选 · 需客服选择"} for r in api]
            except Exception as exc:
                if not choices:
                    raise
                lookup_error = "顺丰反查暂时失败：" + strip_html(str(exc)) + "；保留可核对的历史候选。"
        if not choices:
            raise LabelInputError("顺丰未返回与国家、邮编匹配且省市完整的候选；请核对原地址或联系顺丰核实，不能让 AI 编造地区。", ["address"])
        for index, row in enumerate(choices, 1):
            row["choice_id"] = f"A{index}"
            row["differences"] = {k: {"current": receiver.get(k, ""), "candidate": row.get(k, "")}
                for k in ("province", "city", "county") if str(receiver.get(k) or "").casefold() != str(row.get(k) or "").casefold()}
        token = _store("preparation", {"state": state, "choices": choices,
            "listed_after_message": _last_customer_message(_scope()), "shown_ids": []})
        page = _page(_load("preparation", token), token)
        _mark(steps, stage, "waiting", "已列出真实地址候选；历史地址只推荐，不自动选定。" + (lookup_error or ""))
        missing = []
        receiver_check = {**receiver, **{k: choices[0].get(k, "") for k in ("country", "province", "city", "county", "post_code")}}
        for check in (lambda: validate_parcels(state["parcels"]), lambda: apply_details(receiver_check),
                      lambda: validate_customs(state["customs"])):
            try:
                check()
            except LabelInputError as exc:
                missing.append(str(exc))
        return {"status": "needs_selection", **page, "steps": steps, "source": state,
            "missing": missing, "message": "请选择地址编号；如有缺失，请同时补齐包裹信息。系统申报预设会在最终审核中明确展示。"}
    except Exception as exc:
        return _failure(exc, steps, stage)


def list_sf_label_addresses(preparation_token: str, start: int = 0):
    """分页查看此前顺丰反查返回的真实地址候选，不重新调用接口、不选择地址。"""
    steps = _steps()
    try:
        return {"status": "needs_selection", **_page(_load("preparation", preparation_token), preparation_token, start)}
    except Exception as exc:
        return _failure(exc, steps, "address")




def _set_form(doc, form, parcels):
    doc.set("shipment_parcel", parcels)
    doc.sf_form_json = _json(form)
    doc.service_provider, doc.carrier = "顺丰国际", shipping.SF_PROVIDER
    doc.carrier_service = form["product_name"]
    doc.total_weight, doc.value_of_goods = form["total_weight"], form["declared_value"]
    shipping._ensure_official_fields_from_sf(doc, form)


def preview_sf_label(preparation_token: str, address_choice_id: str, parcels: list | None = None,
                     product_code: str = "", receiver_details: dict | None = None, customs: dict | None = None):
    """客服看到地址候选并明确选择后，生成统一审核摘要，不向顺丰下单。

    parcels 每行 length/width/height 为厘米，weight 为单件千克，count 为件数。
    receiver_details 仅可补充 contact/phone/mobile/email/company/doorplate，不能覆盖地区。
    customs 可覆盖 declared_value/declared_currency/purchase_currency/hs_code/ename/cname。
    """
    steps, stage = _steps(), "source"
    try:
        plan = _load("preparation", preparation_token)
        _, doc, state = _source(plan["state"]["delivery_note"])
        if state is None:
            return get_sf_label_result(doc.name)
        if _hash(state) != _hash(plan["state"]):
            raise LabelInputError("出库单、地址、包裹或设置已变化，请重新准备并选择地址。")
        _mark(steps, stage, "passed", "出库单及地址仍与展示时一致。")
        stage = "address"
        if plan["scope"] != "direct" and _last_customer_message(plan["scope"]) == plan.get("choice_messages", {}).get(address_choice_id, plan["listed_after_message"]):
            raise LabelInputError("客服尚未回复选择地址。请先展示候选并等待回复，即使仅有一条历史地址也不能自动选定。", ["address_choice_id"])
        choice = next((r for r in plan["choices"] if r["choice_id"] == address_choice_id), None)
        if not choice or address_choice_id not in plan["shown_ids"]:
            raise LabelInputError("所选地址不在已展示的真实候选中，请先展示并让客服选择。", ["address_choice_id"])
        receiver = dict(state["receiver"])
        receiver.update({k: choice.get(k, "") for k in ("country", "province", "city", "county", "post_code")})
        # Keep current street and recipient identity, never copy historical contact/phone.
        if customs is not None and not isinstance(customs, dict):
            raise LabelInputError("报关补充信息须为字段对象。", ["customs"])
        errors, fields = [], []
        checks = [lambda: apply_details(receiver, receiver_details),
                  lambda: validate_parcels(state["parcels"] if parcels is None else parcels),
                  lambda: validate_customs({**state["customs"], **(customs or {})})]
        validated = []
        for check in checks:
            try:
                validated.append(check())
            except LabelInputError as exc:
                errors.append(str(exc))
                fields.extend(exc.fields)
                validated.append(None)
        code = str(product_code or state["product_code"])
        product = next((p for p in state["products"] if str(p["product_code"]) == code), None)
        if not product:
            errors.append("请选择真实可用的顺丰产品编号，不能使用未知产品的默认替代值。")
            fields.append("product_code")
        if errors:
            raise LabelInputError("\n".join(errors), fields)
        receiver, parcels, customs = validated
        _mark(steps, stage, "selected", choice["badge"] + "；已采用客服选定的 " + address_choice_id)
        stage = "review"
        totals = parcel_totals(parcels)
        if round(totals["total_weight"], 3) <= 0:
            raise LabelInputError("包裹总重量小于接口精度，请核对千克单位，不使用默认重量。", ["parcels"])
        sender = dict(state["sender"])
        sender["address"] = validate_sf_address(sender.get("address"), "仓库发件详细地址", "sender.address")
        form = {**customs, **totals, "sender": sender, "receiver": receiver,
                "product_code": code, "product_name": product["product_name"]}
        _set_form(doc, form, parcels)
        digest = _payload_hash(doc, form)
        summary = {"delivery_note": state["delivery_note"], "customer": state["customer_name"],
            "goods": state["goods"], "address_choice": choice, "sender": sender, "receiver": receiver,
            "parcels": parcels, "totals": totals, "product": product, "customs": customs,
            "customs_notice": "申报信息来自系统/已有草稿预设及本次补充，请核对实际货物；不是实际运费或收款金额。"}
        token = _store("preview", {"state": state, "form": form, "parcels": parcels,
            "payload_hash": digest, "summary": summary})
        _mark(steps, stage, "waiting", "已完成本地下单参数预检，请整体审核一次；尚未创建运单或请求顺丰下单。")
        return {"status": "preview", "preview_token": token, "summary": summary, "steps": steps,
                "requires_confirmation": True, "message": "请调用 create_sf_label，由系统展示完整摘要并批准一次。"}
    except Exception as exc:
        return _failure(exc, steps, stage)


def _request_name(token):
    _key("preview", token)
    return "flow-sf-" + token


def _existing_request(token):
    name = _request_name(token)
    if not frappe.db.exists("Integration Request", name):
        return None
    ledger = frappe.get_doc("Integration Request", name)
    data = json.loads(ledger.data or "{}")
    if ledger.integration_request_service != SERVICE or (data.get("user"), data.get("scope")) != (_actor(), _scope()):
        raise frappe.PermissionError("此面单请求不属于当前登录人或对话。")
    return get_sf_label_result(ledger.reference_docname)


def _create_sf_label(preview_token: str):
    """经整体批准后保存并提交运单、排队创建顺丰面单和 PDF；不提交出库单。"""
    steps, stage = _steps(), "source"
    point = "flow_sf_" + uuid.uuid4().hex
    frappe.db.savepoint(point)
    try:
        existing = _existing_request(preview_token)
        if existing:
            return existing
        plan = _load("preview", preview_token)
        _, doc, state = _source(plan["state"]["delivery_note"], lock=True)
        # Locking the source DN serializes competing approved plans for that DN.
        existing = _existing_request(preview_token)
        if existing:
            return existing
        if state is None:
            return get_sf_label_result(doc.name)
        if _hash(state) != _hash(plan["state"]):
            raise LabelInputError("审批期间出库单、收发地址、包裹或设置已变化，请重新准备并整体审核。")
        _mark(steps, stage, "passed", "已锁定出库单，复核最新状态与批准方案一致。")
        _mark(steps, "address", "selected", "使用本次客服明确选择的地址。")
        _mark(steps, "review", "approved", "本次完整面单方案已通过系统确认。")
        stage = "booking"
        # Suppress native save hooks until all local checks pass. Queue registration
        # must be LAST: savepoint rollback cannot remove after_commit callbacks.
        doc.flags.flow_sf_defer_booking = True
        _set_form(doc, plan["form"], plan["parcels"])
        if state["shipment"]:
            doc.save()
        else:
            doc.insert()
        doc.submit()
        saved = frappe.get_doc("Shipment", doc.name, for_update=True)
        saved.check_permission("read")
        if saved.docstatus != 1 or _hash(_parcels(saved)) != _hash(plan["parcels"]):
            raise LabelInputError("运单保存后的包裹或状态与批准方案不一致，已撤回本次保存。")
        actual_form = shipping._booking_form_for_doc(saved)
        if _payload_hash(saved, actual_form) != plan["payload_hash"]:
            raise LabelInputError("运单保存后的下单内容与批准方案不一致，已撤回本次保存。")
        from erpnext_shipping.sf_international.waybill import begin_initial_booking_attempt
        attempt = begin_initial_booking_attempt(saved, plan["form"], reason=REASON, commit=False)
        if not attempt:
            raise LabelInputError("未能建立顺丰下单记录，尚未向顺丰发送请求。")
        ledger = frappe.get_doc({"doctype": "Integration Request", "integration_request_service": SERVICE,
            "status": "Queued", "request_id": attempt.name, "request_description": REASON,
            "reference_doctype": "Shipment", "reference_docname": saved.name,
            "data": _json({"user": plan["user"], "scope": plan["scope"], "attempt": attempt.name,
                "plan": plan}), "output": _json({"status": "queued", "reason": "等待后台请求顺丰；尚未取得单号。"})})
        ledger.flags._name = _request_name(preview_token)
        ledger.insert(ignore_permissions=True)
        _mark(steps, stage, "queued", "运单和下单记录已保存，事务提交后后台请求顺丰；现在还没有创建成功的单号。")
        result = {"status": "queued", "verified": False, "shipment": saved.name,
            "url": "/app/shipment/" + quote(saved.name, safe=""), "steps": steps,
            "message": "已排队。接下来只读取结果，不能再次创建；取得单号与 PDF 后分别显示。"}
        frappe.enqueue(finish_sf_label, queue="short", timeout=300, enqueue_after_commit=True,
            job_id="flow-sf-label:" + saved.name, deduplicate=True, request_name=ledger.name)
        return result
    except Exception as exc:
        _rollback(point)
        result = _failure(exc, steps, stage)
        result["rolled_back"] = True
        return result


def _save_result(ledger, status, reason):
    ledger.output = _json({"status": status, "reason": strip_html(str(reason))[:1500]})
    ledger.status = "Completed" if status in ("ready", "created_without_pdf") else "Failed" if status in ("uncertain", "needs_review") else "Queued"
    ledger.save(ignore_permissions=True)




def finish_sf_label(request_name):
    """One durable claim per approval; a crashed/uncertain POST is never retried."""
    ledger = frappe.get_doc("Integration Request", request_name, for_update=True)
    if ledger.integration_request_service != SERVICE:
        raise frappe.PermissionError("不是顺丰面单流程请求。")
    data = json.loads(ledger.data)
    if data["user"] != _actor():
        raise frappe.PermissionError("下单任务执行人不匹配。")
    outcome = json.loads(ledger.output or "{}")
    if outcome.get("status") != "queued":
        return outcome
    _save_result(ledger, "processing", "后台开始核对并请求顺丰；若任务中断，必须核对原下单记录，不自动重下。")
    frappe.db.commit()  # Durable claim BEFORE any external side effect.
    previous = frappe.flags.get("flow_sf_active_request")
    frappe.flags.flow_sf_active_request = ledger.name
    try:
        doc = frappe.get_doc("Shipment", ledger.reference_docname, for_update=True)
        attempt = frappe.get_doc("SF Waybill", data["attempt"])
        form = json.loads(attempt.form_payload)
        form["sender"] = shipping._sender_from_warehouse(doc=doc, pickup_address_name=doc.get("pickup_address_name"))
        validate_reviewed_booking(doc, form, attempt)
        waybill = shipping.book_sf_order_after_commit(doc.name, attempt_name=attempt.name)
        if not waybill:
            raise LabelInputError("运单已被其他操作变更，后台没有取得单号，请核对原运单。")
    except Exception as exc:
        frappe.db.rollback()
        # Native booking retains any external response/uncertainty durably.
        ledger = frappe.get_doc("Integration Request", request_name)
        status = "needs_review" if isinstance(exc, LabelInputError) else "uncertain"
        _save_result(ledger, status, str(exc) + " 不会自动再次下单，请核对原运单及面单历史。")
        frappe.db.commit()
        return json.loads(ledger.output)
    finally:
        frappe.flags.flow_sf_active_request = previous
    try:
        shipping.print_sf_label(ledger.reference_docname)
        _save_result(ledger, "ready", "顺丰已返回真实单号，面单 PDF 已保存，可打开核对。")
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        ledger = frappe.get_doc("Integration Request", request_name)
        _save_result(ledger, "created_without_pdf", "顺丰单号已创建，PDF 暂未生成：" + strip_html(str(exc)) + "；请从原运单打印面单，不要重新下单。")
        frappe.db.commit()
    return json.loads(ledger.output)


def get_sf_label_result(shipment: str):
    """只读取原运单、下单记录和已保存 PDF，不再次下单、打印或查询收费接口。"""
    steps = _steps()
    try:
        _actor()
        doc = frappe.get_doc("Shipment", shipment)
        doc.check_permission("read")
        if not shipping._is_sf_shipment(doc):
            raise LabelInputError("这不是顺丰运单，请在原运单中查看对应物流公司的结果。")
        _mark(steps, "source", "passed", "已读取当前账号有权查看的原运单。")
        name = frappe.db.get_value("Integration Request", {"integration_request_service": SERVICE,
            "reference_doctype": "Shipment", "reference_docname": doc.name}, "name", order_by="creation desc")
        outcome = {}
        if name:
            ledger = frappe.get_doc("Integration Request", name)
            # Shipment read permission grants its operation result, not cached
            # approval tokens, private settings, other users' chat or raw payloads.
            outcome = json.loads(ledger.output or "{}")
        waybill = shipping._sf_waybill(doc)
        url = str(doc.get("sf_label_url") or "")
        file_url = url if url.startswith(("/files/", "/private/files/")) else None
        if doc.docstatus == 2 or shipping._is_cancelled(doc) or doc.get("sf_carrier_cancelled"):
            status, reason = "cancelled", "该运单已取消，请查看原单历史，不能将旧面单作为本次成功结果。"
        elif waybill:
            status = "ready" if file_url else "created_without_pdf"
            reason = "顺丰已返回单号，PDF 已保存。" if file_url else "顺丰已返回单号；PDF 尚未就绪，可稍后从原运单打印，不要重新下单。"
            if outcome.get("status") == "created_without_pdf":
                reason = outcome["reason"]
            _mark(steps, "booking", "completed", "真实顺丰单号：" + waybill + "；创建面单不等于包裹已发出。")
            _mark(steps, "label", "completed" if file_url else "pending", reason)
        else:
            status = outcome.get("status") or ("processing" if doc.get("sf_waybill_pending_record") else "not_started")
            reason = outcome.get("reason") or doc.get("sf_waybill_replacement_note") or "尚未取得顺丰单号。"
            if status in ("ready", "created_without_pdf"):
                status, reason = "uncertain", "下单记录与运单当前单号不一致，请核对面单历史，不要重新下单。"
            pending = doc.get("sf_waybill_pending_record")
            if pending:
                attempt = frappe.get_doc("SF Waybill", pending)
                if attempt.get("shipment") == doc.name and attempt.get("creation_uncertain"):
                    status = "uncertain"
                    reason = attempt.get("creation_error") or "顺丰下单结果待核对，请查看原运单记录，不要再次下单。"
            _mark(steps, "booking", status, reason)
        if name:
            _mark(steps, "address", "selected", "已保存本次地址选择。")
            _mark(steps, "review", "approved", "已保存客服批准的完整方案。")
        return {"status": status, "verified": bool(waybill) and status != "cancelled", "shipment": doc.name,
            "waybill": waybill or None, "label_url": file_url, "url": "/app/shipment/" + quote(doc.name, safe=""),
            "reason": reason, "steps": steps, "next_action": "打开面单核对" if status == "ready" else
            "继续查看原运单结果；不重复创建。"}
    except Exception as exc:
        return _failure(exc, steps, "source")


def _dispatch_confirmation(args):
    """Build the final dispatch confirmation from the current Shipment state."""
    try:
        shipment = str((args or {}).get("shipment") or "").strip()
        if not shipment:
            return "🔴 缺少系统运单编号，不能确认发货。"
        doc = frappe.get_doc("Shipment", shipment)
        doc.check_permission("read")
        if not shipping._is_sf_shipment(doc):
            return "🔴 这不是顺丰运单，不能用顺丰面单发货工具。"
        waybill = shipping._sf_waybill(doc)
        if not waybill:
            return "🔴 当前运单还没有真实顺丰单号，不能确认发货。"
        return "\n".join([
            "🔵 请确认已打印面单并发货",
            "",
            "系统运单：" + doc.name,
            "顺丰单号：" + waybill,
            "当前状态：" + str(doc.get("status") or "未标记发货"),
            "",
            "批准后系统会读取并保存面单，然后把当前运单标记为已发货。",
            "已发货的运单再次执行只返回当前状态，不会重复请求顺丰。",
            "请确认包裹确实已交给物流后批准一次。",
        ])
    except Exception as exc:
        return "🔴 当前运单不能确认发货：" + strip_html(str(exc))


def _dispatch_sf_label(shipment: str):
    """After one approval, print the existing label and mark the shipment shipped.

    The carrier implementation owns the native status transition.  This Flow
    boundary only verifies the current waybill, delegates the mutation once,
    and reads the saved document back before reporting success.
    """
    dispatch_started = False
    try:
        _actor()
        doc = frappe.get_doc("Shipment", shipment, for_update=True)
        doc.check_permission("write")
        if not shipping._is_sf_shipment(doc):
            raise LabelInputError("这不是顺丰运单，不能执行顺丰发货。", ["shipment"])
        if doc.docstatus != 1:
            raise LabelInputError("运单必须先提交后才能发货；当前没有修改任何状态。", ["shipment"])
        if shipping._is_cancelled(doc) or doc.get("sf_carrier_cancelled"):
            raise LabelInputError("运单已取消，不能再次发货；原面单历史保持不变。", ["shipment"])
        waybill = str(shipping._sf_waybill(doc) or "").strip()
        if not waybill:
            raise LabelInputError("当前运单还没有真实顺丰单号，不能发货；请先完成面单创建并核对结果。", ["shipment"])

        from erpnext_shipping.sf_international import waybill as waybill_api
        if waybill_api._shipment_is_shipped(doc):
            return {"status": "already_shipped", "verified": True, "shipment": doc.name,
                    "waybill": waybill, "message": "该运单已经是已发货状态，没有重复打印或请求顺丰。"}

        dispatch_started = True
        label_url = shipping.dispatch_sf_shipment(doc.name)
        saved = frappe.get_doc("Shipment", doc.name)
        saved.check_permission("read")
        saved_waybill = str(shipping._sf_waybill(saved) or "").strip()
        if saved_waybill != waybill:
            raise LabelInputError("发货后当前顺丰单号发生变化，未确认本次状态；请核对原运单历史。", ["shipment"])
        if not waybill_api._shipment_is_shipped(saved):
            raise LabelInputError("顺丰面单调用已返回，但系统回读仍不是已发货；请核对原运单，不要重复操作。", ["shipment"])
        return {"status": "dispatched", "verified": True, "shipment": saved.name,
                "waybill": saved_waybill, "label_url": label_url or saved.get("sf_label_url"),
                "url": "/app/shipment/" + quote(saved.name, safe=""),
                "message": "已读取现有顺丰面单并确认运单已发货。后续查询物流请使用当前或历史单号。"}
    except Exception as exc:
        if dispatch_started:
            # The native carrier call may have printed the label or persisted
            # the status before the read-back failed.  Never report that as a
            # clean failure which invites a second dispatch attempt.
            try:
                current = frappe.get_doc("Shipment", shipment)
                from erpnext_shipping.sf_international import waybill as waybill_api
                if waybill_api._shipment_is_shipped(current):
                    return {"status": "already_shipped", "verified": True, "shipment": current.name,
                            "waybill": str(shipping._sf_waybill(current) or "").strip(),
                            "message": "原生发货调用后回读到运单已发货，没有重复操作。"}
            except Exception:
                pass
            return {"status": "uncertain", "verified": False, "shipment": shipment,
                    "reason": strip_html(str(exc)),
                    "message": "发货请求已经发起，但当前状态暂未核实；请先打开原运单确认，不要重复点击发货。"}
        return {"status": "error", "verified": False, "shipment": shipment,
                "reason": strip_html(str(exc)),
                "message": "发货未完成；系统没有把失败当作已发货，请按原因核对原运单。"}


def _confirmation_prompt(args):
    try:
        s = _load("preview", args.get("preview_token"))["summary"]
        r, sender, c = s["receiver"], s["sender"], s["customs"]
        lines = ["🔵 请整体审核：创建顺丰面单并生成 PDF", "",
            "出库单：" + s["delivery_note"], "客户：" + s["customer"], "",
            s["address_choice"]["badge"], "客服选择：" + s["address_choice"]["choice_id"],
            "收件人：" + r["contact"] + "；电话：" + r["phone"],
            "收货地址：" + " / ".join(str(r.get(k) or "") for k in ("country", "province", "city", "county", "address", "doorplate")),
            "邮编：" + r["post_code"], "", "【本次商品】"]
        for row in s["goods"]:
            lines.append(f"• {row['item_name']}：{row['qty']} {row['uom']}；仓库：{row['warehouse']}")
        lines += ["", "【包裹】"]
        for row in s["parcels"]:
            lines.append(f"• {row['count']} 件；每件 {row['length']:g} × {row['width']:g} × {row['height']:g} 厘米，{row['weight']:g} 千克")
        lines += [f"合计：{s['totals']['parcel_quantity']} 件，{s['totals']['total_weight']:g} 千克",
            "顺丰产品：" + s["product"]["product_name"] + "（" + str(s["product"]["product_code"]) + "）",
            "发件人：" + str(sender.get("company") or "") + " / " + str(sender.get("contact") or "") + " / " + str(sender.get("phone") or sender.get("mobile") or ""),
            "发件地址：" + " / ".join(str(sender.get(k) or "") for k in ("country", "province", "city", "county", "address", "post_code")),
            "", "🟠【申报信息，请核对实际货物】", f"申报金额：{c['declared_currency']} {c['declared_value']:g}；采购币种：{c['purchase_currency']}",
            f"HS 编码：{c['hs_code']}；品名：{c['cname']} / {c['ename']}", s["customs_notice"],
            "", "批准后保存并提交运单，后台向顺丰创建真实面单并生成 PDF。此操作不表示包裹已经发出。",
            "正确请批准一次；需要修改请拒绝并说明。"]
        if s["address_choice"].get("warning"):
            lines.append("🟠 " + s["address_choice"]["warning"])
        if s["address_choice"].get("differences"):
            lines.append("🟠 候选地区与档案文字存在差异，请核对：")
            labels = {"province": "省/州", "city": "城市", "county": "区/县"}
            for key, diff in s["address_choice"]["differences"].items():
                lines.append(f"• {labels[key]}：档案 {diff['current'] or '未填写'} → 本次 {diff['candidate'] or '未填写'}")
        return "\n".join(lines)
    except Exception as exc:
        return "当前面单方案不能执行：" + strip_html(str(exc))


from flow.lib.tool import tool
create_sf_label = tool(_create_sf_label, name="create_sf_label", requires_confirmation=True,
                      confirm_prompt=_confirmation_prompt)
dispatch_sf_label = tool(_dispatch_sf_label, name="dispatch_sf_label", requires_confirmation=True,
                         confirm_prompt=_dispatch_confirmation)
