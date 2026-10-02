"""Offline contracts for reviewed native Sales Order/Delivery Note submission."""
from copy import deepcopy
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


class Doc(dict):
	def __getattr__(self, key):
		return self.get(key)

	def __setattr__(self, key, value):
		self[key] = value

	def check_permission(self, permission):
		if permission in self.get("denied", set()):
			raise PermissionError("缺少 " + permission + " 权限")

	def submit(self):
		if self.get("submit_error"):
			raise RuntimeError(self["submit_error"])
		self.docstatus = 1
		self.status = "Submitted"


@pytest.fixture
def env(monkeypatch):
	state = Doc(user="sales@example.com", scope="chat-1", cache={}, calls=0)
	frappe = types.ModuleType("frappe")
	frappe.PermissionError = PermissionError
	frappe.local = Doc(site="test.local")
	frappe.session = Doc(user=state.user, sid=state.scope)
	frappe.flags = Doc()
	frappe.cache = Doc(
		get_value=lambda key: deepcopy(state.cache.get(key)),
		set_value=lambda key, value, **_: state.cache.__setitem__(key, deepcopy(value)),
	)
	frappe.db = Doc(
		savepoint=lambda name: state.update(savepoint=name),
		rollback=lambda **kwargs: state.update(rollback=kwargs),
	)
	frappe.scrub = lambda value: value.lower().replace(" ", "-")
	frappe.utils = types.SimpleNamespace(
		get_datetime=lambda value: __import__("datetime").datetime.fromisoformat(value),
		now_datetime=lambda: __import__("datetime").datetime(2026, 9, 30, 12),
		strip_html=str,
	)
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	utils = types.ModuleType("frappe.utils")
	utils.get_datetime = frappe.utils.get_datetime
	utils.now_datetime = frappe.utils.now_datetime
	utils.strip_html = str
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)
	flow_tool = types.ModuleType("flow.lib.tool")
	flow_tool.tool = lambda fn, **_: fn
	monkeypatch.setitem(sys.modules, "flow.lib.tool", flow_tool)
	sales = types.ModuleType("flow.integrations.erpnext.sales_order_flow")
	sales._actor = lambda: state.user
	sales._hash = lambda value: __import__("hashlib").sha256(repr(value).encode()).hexdigest()
	monkeypatch.setitem(sys.modules, "flow.integrations.erpnext.sales_order_flow", sales)

	doc = Doc(
		doctype="Sales Order", name="SAL-ORD-1", customer="CUST-1", customer_name="客户",
		company="Company", currency="USD", status="Draft", docstatus=0,
		modified="2026-09-30 11:00:00", transaction_date="2026-09-30", grand_total=120,
		items=[Doc(idx=1, item_code="ITEM-1", item_name="巧克粉", qty=10, uom="Nos", warehouse="WH", rate=12, amount=120)],
	)
	def get_doc(doctype, name, **kwargs):
		state.last_get_doc_kwargs = kwargs
		return doc
	frappe.get_doc = get_doc
	state.update(doc=doc, module=None, frappe=frappe)
	spec = importlib.util.spec_from_file_location("flow.integrations.erpnext.document_submission", ROOT / "flow/integrations/erpnext/document_submission.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	state.module = module
	return state


def test_preview_does_not_submit_and_returns_bound_token(env):
	result = env.module.preview_document_submission("Sales Order", "SAL-ORD-1")
	assert result["status"] == "preview"
	assert result["summary"]["items"][0]["qty"] == 10
	assert env.doc.docstatus == 0
	assert len(result["submission_token"]) == 32
	assert result["submission_token"] != result["summary"]["modified"][:32]
	assert env.last_get_doc_kwargs == {"for_update": False}


def test_submit_requires_current_revision_and_does_not_call_native_submit_on_change(env):
	preview = env.module.preview_document_submission("Sales Order", "SAL-ORD-1")
	env.doc.modified = "2026-09-30 11:01:00"
	result = env.module.submit_reviewed_document(preview["submission_token"])
	assert result["status"] == "needs_review"
	assert env.doc.docstatus == 0


def test_submit_uses_native_submit_once_and_replay_is_idempotent(env):
	preview = env.module.preview_document_submission("Sales Order", "SAL-ORD-1")
	result = env.module.submit_reviewed_document(preview["submission_token"])
	assert result["status"] == "submitted" and result["verified"]
	assert env.doc.docstatus == 1
	assert "rollback" not in env
	replay = env.module.submit_reviewed_document(preview["submission_token"])
	assert replay["status"] == "needs_review"
	assert env.last_get_doc_kwargs == {"for_update": True}
	current = env.module.preview_document_submission("Sales Order", "SAL-ORD-1")
	assert current["status"] == "already_submitted" and current["verified"]


def test_submit_failure_returns_reason_and_keeps_draft(env):
	preview = env.module.preview_document_submission("Sales Order", "SAL-ORD-1")
	env.doc.submit_error = "原生校验失败"
	result = env.module.submit_reviewed_document(preview["submission_token"])
	assert result["status"] == "error" and "原生校验失败" in result["reason"]
	assert env.doc.docstatus == 0
	assert env.rollback == {"save_point": env.savepoint}


def test_preview_failure_does_not_rollback_callers_transaction(env):
	env.doc.denied = {"submit"}
	result = env.module.preview_document_submission("Sales Order", "SAL-ORD-1")
	assert result["status"] == "error"
	assert "rollback" not in env


@pytest.mark.parametrize("change", ["user", "scope"])
def test_token_is_bound_to_user_and_session(env, change):
	preview = env.module.preview_document_submission("Sales Order", "SAL-ORD-1")
	env[change] = "other"
	if change == "user":
		env.frappe.session.user = "other"
	else:
		env.frappe.session.sid = "other"
	result = env.module.submit_reviewed_document(preview["submission_token"])
	assert result["status"] == "error"
	assert env.doc.docstatus == 0


def test_cancelled_document_is_rejected(env):
	env.doc.docstatus = 2
	result = env.module.preview_document_submission("Sales Order", "SAL-ORD-1")
	assert result["status"] == "needs_input"
	assert env.doc.docstatus == 2


def test_only_supported_document_types_are_accepted(env):
	result = env.module.preview_document_submission("Customer", "CUST-1")
	assert result["status"] == "needs_input"
