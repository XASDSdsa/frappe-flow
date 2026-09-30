"""Reviewed native financial writes for Flow.

Only the internal idempotency journal bypasses document permissions. The caller
owns commit; preview and failed writes restore SQL and pending callbacks.
"""
from copy import deepcopy
from datetime import timedelta
import html
import json
import re
import uuid
from urllib.parse import quote

import frappe
from frappe.utils import now_datetime, get_datetime, strip_html

from .sales_order_flow import _actor, _scope, _hash, _json, _without_price_maintenance

VERSION = 1
TTL = 1800
KINDS = {
    "purchase_order": "Purchase Order",
    "purchase_receipt": "Purchase Receipt",
    "sales_invoice": "Sales Invoice",
}


class InputError(Exception):
    def __init__(self, message, fields=None):
        super().__init__(message)
        self.fields = fields or []


class Boundary:
    def __init__(self):
        self.name = "reviewed_" + uuid.uuid4().hex
        self.callbacks = {key: list(getattr(frappe.db, key)._functions)
            for key in ("before_commit", "after_commit", "before_rollback", "after_rollback")}
        self.realtime = deepcopy(getattr(frappe.local, "_realtime_log", None))
        frappe.db.savepoint(self.name)

    def rollback(self):
        try:
            frappe.db.rollback(save_point=self.name)
        except Exception:
            frappe.db.rollback()
            raise RuntimeError("数据库事务已整体回滚，请重新读取原单状态后继续。")
        for key, pending in self.callbacks.items():
            manager = getattr(frappe.db, key)
            manager.reset()
            for callback in pending:
                manager.add(callback)
        if self.realtime is None:
            if hasattr(frappe.local, "_realtime_log"):
                del frappe.local._realtime_log
        else:
            frappe.local._realtime_log = self.realtime
        if hasattr(frappe.local, "document_cache"):
            frappe.local.document_cache.clear()


def _key(kind, token):
    if kind not in KINDS or not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise InputError("审核方案编号无效，请重新预检。", ["preview_token"])
    return "flow-reviewed-" + kind + "-" + token


def _steps():
    return [{"key": key, "name": label, "status": "not_run", "reason": "前序步骤尚未完成，未执行。"}
        for key, label in (("source", "核对来源与权限"), ("review", "核对审核内容"),
                          ("save", "保存原生单据"), ("submit", "按批准内容提交"), ("verify", "回读实际结果"))]


def _mark(steps, key, status, reason):
    next(s for s in steps if s["key"] == key).update(status=status, reason=reason)


def failure(exc, steps=None, stage="source", rolled_back=False):
    steps = steps or _steps()
    reason = strip_html(str(exc)).strip() or type(exc).__name__
    if rolled_back:
        for step in steps:
            if step["status"] == "completed":
                step.update(status="rolled_back", reason="后续核验失败，本次写入已撤回。")
    _mark(steps, stage, "failed", reason)
    return {"status": "needs_input" if isinstance(exc, InputError) else "error", "verified": False,
        "reason": reason, "missing": getattr(exc, "fields", []), "steps": steps,
        "failed_step": stage, "rolled_back": rolled_back,
        "message": ("本次写入已撤回。" if rolled_back else "未保存任何单据。") + reason}


def _load(kind, token):
    key = _key(kind, token)
    plan = frappe.cache.get_value(key)
    if not plan or plan.get("version") != VERSION or now_datetime() > get_datetime(plan["expires_at"]):
        raise InputError("审核方案已过期，请重新预检并审核。", ["preview_token"])
    if (plan.get("user"), plan.get("scope"), plan.get("site"), plan.get("kind")) != (
            _actor(), _scope(), frappe.local.site, kind):
        raise frappe.PermissionError("此审核方案不属于当前账号或对话。")
    return plan


def _permissions(doc, request):
    doc.flags.ignore_permissions = False
    doc.check_permission("create" if doc.is_new() else "write")
    if doc.docstatus != 0:
        raise InputError("只能处理新单据或原草稿；已提交、已取消单据不能重复保存提交。")
    if request.get("submit"):
        doc.check_permission("submit")


def _existing(kind, token, summarize, steps):
    name = _key(kind, token)
    if not frappe.db.exists("Integration Request", name):
        return None
    journal = frappe.get_doc("Integration Request", name, for_update=True)
    data = json.loads(journal.data or "{}")
    if (journal.integration_request_service, data.get("user"), data.get("scope"), data.get("site")) != (
            "Flow Reviewed " + kind, _actor(), _scope(), frappe.local.site):
        raise frappe.PermissionError("此执行记录不属于当前账号或对话。")
    if journal.status != "Completed" or journal.reference_doctype != KINDS[kind] or not journal.reference_docname:
        raise InputError("本方案仍在处理，请先核对原记录，不能重复创建。")
    doc = frappe.get_doc(journal.reference_doctype, journal.reference_docname)
    doc.check_permission("read")
    if doc.docstatus == 2:
        raise InputError("本方案对应的原单已经取消，不能重复使用此批准方案。")
    summary = summarize(doc, data["request"])
    unchanged = _hash(summary) == data["summary_hash"] and doc.docstatus == data["docstatus"]
    for step in steps:
        step.update(status="reused", reason="返回本方案对应的原单据，未重复写入或提交。")
    return {"status": "existing" if unchanged else "existing_changed", "verified": unchanged,
        "doctype": doc.doctype, "document": doc.name, "docstatus": doc.docstatus,
        "url": _url(doc), "summary": summary, "steps": steps,
        "reason": "本方案已经执行，返回原单。" if unchanged else "原单执行后已变化，请核对当前单据；未重新执行。"}


def _url(doc):
    return "/app/" + frappe.scrub(doc.doctype).replace("_", "-") + "/" + quote(doc.name, safe="")


def preview(kind, request, build, summarize):
    boundary, steps = Boundary(), _steps()
    try:
        user, scope = _actor(), _scope()
        if kind not in KINDS or not isinstance(request, dict):
            raise InputError("预检参数无效。")
        request = deepcopy(request)
        request.setdefault("submit", False)
        if type(request["submit"]) is not bool or type(request.get("new_request", False)) is not bool:
            raise InputError("提交与另开新单标记必须为布尔值。")
        with _without_price_maintenance():
            doc = build(request, for_update=False)
        if doc.doctype != KINDS[kind]:
            raise InputError("预检单据类型不一致。")
        _permissions(doc, request)
        summary = summarize(doc, request)
        _mark(steps, "source", "passed", "已核对真实来源、当前账号权限和原生单据规则。")
        _mark(steps, "review", "needs_confirmation", "请核对完整审核卡，批准一次后执行所示保存或提交操作。")
        fingerprint = _hash({"version": VERSION, "kind": kind, "site": frappe.local.site,
            "user": user, "scope": scope, "request": request, "summary": summary})
        token = uuid.uuid4().hex if request.get("new_request") else fingerprint[:32]
        existing = _existing(kind, token, summarize, steps)
        if existing:
            return existing
        plan = {"version": VERSION, "kind": kind, "site": frappe.local.site, "user": user,
            "scope": scope, "request": request, "summary": summary, "fingerprint": fingerprint,
            "expires_at": str(now_datetime() + timedelta(seconds=TTL))}
        frappe.cache.set_value(_key(kind, token), plan, expires_in_sec=TTL)
        return {"status": "preview", "verified": False, "preview_token": token, "summary": summary,
            "steps": steps, "expires_at": plan["expires_at"], "reason": "预检完成，尚未保存；请核对系统审核卡。"}
    except Exception as exc:
        return failure(exc, steps)
    finally:
        boundary.rollback()


def execute(kind, preview_token, build, summarize, verify=None):
    boundary, steps, stage = Boundary(), _steps(), "source"
    try:
        _actor()
        prior = _existing(kind, preview_token, summarize, steps)
        if prior:
            return prior
        plan = _load(kind, preview_token)
        journal = frappe.get_doc({"doctype": "Integration Request", "integration_request_service": "Flow Reviewed " + kind,
            "request_id": plan["fingerprint"], "status": "Queued",
            "data": _json({k: plan[k] for k in ("user", "scope", "site", "request")}),
            "request_description": "Flow 整体审核后执行"})
        journal.flags._name = _key(kind, preview_token)
        try:
            journal.insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            boundary.rollback()
            return _existing(kind, preview_token, summarize, steps)
        with _without_price_maintenance():
            doc = build(plan["request"], for_update=True)
            if doc.doctype != KINDS[kind]:
                raise InputError("执行单据类型与批准内容不一致。")
            _permissions(doc, plan["request"])
            _mark(steps, "source", "passed", "已重新锁定并核对来源、可用数量与当前权限。")
            stage = "review"
            if _hash(summarize(doc, plan["request"])) != _hash(plan["summary"]):
                raise InputError("数量、金额、来源或配置已变化，请重新预检并审核新内容。", ["preview_token"])
            _mark(steps, "review", "passed", "执行内容与本次整体批准的审核卡一致。")
            stage = "save"
            doc.insert() if doc.is_new() else doc.save()
            _mark(steps, "save", "completed", "单据已通过原生校验保存，正在完成本次核验。")
            saved = frappe.get_doc(doc.doctype, doc.name)
            saved.check_permission("read")
            if _hash(summarize(saved, plan["request"])) != _hash(plan["summary"]):
                raise InputError("保存后的内容与已审核方案不一致。")
            stage = "submit"
            if plan["request"].get("submit"):
                saved.check_permission("submit")
                saved.submit()
                _mark(steps, "submit", "completed", "已按整体批准内容完成原生提交。")
            else:
                _mark(steps, "submit", "skipped", "本次批准仅保存草稿，未执行提交。")
        stage = "verify"
        saved = frappe.get_doc(doc.doctype, doc.name)
        saved.check_permission("read")
        expected = int(plan["request"].get("submit", False))
        if saved.docstatus != expected or _hash(summarize(saved, plan["request"])) != _hash(plan["summary"]):
            raise InputError("回读状态或内容与本次批准不一致。")
        if verify:
            verify(saved, plan["request"])
        _mark(steps, "verify", "passed", "已回读单据状态、数量金额及本次要求的流水证据。")
        result = {"status": "submitted" if expected else "draft", "verified": True,
            "doctype": saved.doctype, "document": saved.name, "docstatus": saved.docstatus, "url": _url(saved),
            "summary": summarize(saved, plan["request"]), "steps": steps,
            "reason": "已提交并完成实际结果核验。" if expected else "草稿已保存并核验；尚未提交。"}
        data = json.loads(journal.data)
        data.update(summary_hash=_hash(result["summary"]), docstatus=saved.docstatus)
        journal.update({"status": "Completed", "data": _json(data), "reference_doctype": saved.doctype,
            "reference_docname": saved.name, "output": _json(result)})
        journal.save(ignore_permissions=True)
        return result
    except Exception as exc:
        boundary.rollback()
        return failure(exc, steps, stage, rolled_back=True)


def confirmation(kind, preview_token):
    try:
        plan = _load(kind, preview_token)
    except Exception as exc:
        return "🔴 **方案不可执行**\n\n" + html.escape(strip_html(str(exc))) + "\n\n请拒绝本次操作并重新预检。"
    summary = plan["summary"]
    safe = lambda x: html.escape(strip_html(str(x))).replace("|", "／").replace("\n", " ")
    lines = ["**" + safe(summary.get("title", "整体审核")) + "**", "",
             "**本次操作：" + safe(summary.get("action", "提交" if plan["request"]["submit"] else "保存草稿")) + "**"]
    for warning in summary.get("warnings", []):
        lines.extend(["", "⚠️ " + safe(warning)])
    for label, value in summary.get("fields", {}).items():
        lines.append(safe(label) + "：" + safe(value))
    rows = summary.get("items", [])
    if rows:
        headers = list(dict.fromkeys(k for row in rows for k in row))
        lines.extend(["", "| " + " | ".join(map(safe, headers)) + " |", "| " + " | ".join("---" for _ in headers) + " |"])
        lines.extend("| " + " | ".join(safe(row.get(k, "")) for k in headers) + " |" for row in rows)
    for label, value in summary.get("totals", {}).items():
        lines.extend(["", "**" + safe(label) + "：" + safe(value) + "**"])
    for effect in summary.get("effects", []):
        lines.extend(["", safe(effect)])
    lines.extend(["", "核对无误后批准一次；需修改请选择拒绝并说明修改内容。"])
    return "\n".join(lines)
