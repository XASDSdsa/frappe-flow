# Unstick a Flow session so the next message can run.
# GPT-6-Astra (and other reasoning models) reject Flow's long tool docs as
# an invalid prompt; shorten descriptions before they leave for LiteLLM.
# Streaming also needs a heartbeat: reasoning models can sit silent for minutes,
# and the browser then reports "network error" on the dead SSE socket.

from __future__ import annotations

import json
import queue
import re
import threading
import time
from typing import Any

import frappe
from frappe import _

_TOOL_DESC_LIMIT = 160
_STREAM_TIMEOUT = 180
_HEARTBEAT_SECONDS = 12
# execute only returns the `result` variable; the full docstring that says so
# is too long for GPT-6-Astra, so keep that one rule after truncating.
_TOOL_DESC_HINTS = {
	"execute": " Assign the return value to a variable named `result`. Example: result = frappe.utils.now_datetime()",
}


def _compact_tool_description(name: str, text: str) -> str:
	line = next((part.strip() for part in text.replace("\r", "").split("\n") if part.strip()), "")
	if len(line) > _TOOL_DESC_LIMIT:
		line = line[:_TOOL_DESC_LIMIT].rstrip() + "…"
	hint = _TOOL_DESC_HINTS.get(name or "")
	if hint and "result" not in line:
		line = line.rstrip("…") + hint
	return line


def _compact_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
	if not tools:
		return tools
	compacted: list[dict[str, Any]] = []
	for tool in tools:
		if not isinstance(tool, dict):
			compacted.append(tool)
			continue
		function = tool.get("function")
		if not isinstance(function, dict):
			compacted.append(tool)
			continue
		description = function.get("description")
		if not isinstance(description, str) or not description.strip():
			compacted.append(tool)
			continue
		short = _compact_tool_description(str(function.get("name") or ""), description)
		if short == description:
			compacted.append(tool)
			continue
		compacted.append({**tool, "function": {**function, "description": short}})
	return compacted


def _heartbeat_stream(chunks, interval: float = _HEARTBEAT_SECONDS):
	"""Keep the SSE socket alive while LiteLLM waits on the first token.

	Empty string deltas are ignored by the panel but still flush as `text` events.
	"""
	pending: queue.Queue = queue.Queue()

	def reader():
		try:
			while True:
				pending.put(("chunk", next(chunks)))
		except StopIteration as done:
			pending.put(("return", done.value))
		except BaseException as err:
			pending.put(("error", err))

	threading.Thread(target=reader, daemon=True, name="flow-llm-stream").start()
	while True:
		try:
			kind, payload = pending.get(timeout=interval)
		except queue.Empty:
			yield ""
			continue
		if kind == "chunk":
			yield payload
		elif kind == "return":
			return payload
		else:
			raise payload


_TOOL_HISTORY_KEEP = 6
_TOOL_HISTORY_LIMIT = 2000
_TOOL_HISTORY_KEEP_LIMIT = 4000
# These tools return a complete business document by contract. Truncating their
# JSON changes the data the model is asked to summarize, so preserve the result
# while keeping the normal budget for exploratory and legacy tools.
_COMPLETE_TOOL_HISTORY_NAMES = frozenset({"query_sales_order_details"})
_DESCRIBE_FIELD_CAP = 28
_DESCRIBE_KEEP_FIELDS = {
	"name",
	"naming_series",
	"customer",
	"company",
	"transaction_date",
	"delivery_date",
	"status",
	"docstatus",
	"sales_partner",
	"sales_team",
	"sales_person",
	"commission_rate",
	"order_type",
	"currency",
	"owner",
}
_DESCRIBE_KEEP_TOKENS = ("sales", "team", "person", "partner", "commission")


def _trim_describe_field(field: dict[str, Any]) -> dict[str, Any]:
	out = dict(field)
	options = out.get("options")
	if isinstance(options, str) and len(options) > 80:
		out["options"] = options[:80].rstrip() + "…"
	return out


def _slim_describe(result: Any) -> Any:
	"""Sales Order meta is 200+ fields; dumping it freezes the panel on 'Reading DocType Meta'."""
	if not isinstance(result, dict):
		return result
	fields = result.get("fields")
	if not isinstance(fields, list) or len(fields) <= _DESCRIBE_FIELD_CAP:
		return result
	kept: list[dict[str, Any]] = []
	child_tables: list[dict[str, Any]] = []
	seen: set[str] = set()

	def take(field: dict[str, Any]) -> None:
		name = field.get("fieldname")
		if not name or name in seen:
			return
		seen.add(name)
		kept.append(_trim_describe_field(field))

	for field in fields:
		if not isinstance(field, dict):
			continue
		if (field.get("type") or "") == "Table":
			child_tables.append(
				{
					"fieldname": field.get("fieldname"),
					"label": field.get("label"),
					"options": field.get("options"),
				}
			)
			take(field)
	for field in fields:
		if not isinstance(field, dict):
			continue
		name = str(field.get("fieldname") or "")
		lower = name.lower()
		if field.get("required") or name in _DESCRIBE_KEEP_FIELDS or any(
			token in lower for token in _DESCRIBE_KEEP_TOKENS
		):
			take(field)
	out = dict(result)
	out["fields"] = kept
	out["child_tables"] = child_tables
	out["field_count"] = len(fields)
	out["note"] = (
		f"Showing {len(kept)} of {len(fields)} fields (required, tables, sales-related). "
		"Child tables are under child_tables; find_doctypes does not list them."
	)
	return out


def _slim_describe_json(content: str) -> str:
	try:
		data = json.loads(content)
	except Exception:
		return content
	slim = _slim_describe(data)
	if slim is data:
		return content
	return json.dumps(slim, ensure_ascii=False)


def _compact_tool_history(messages: Any) -> Any:
	"""Old tool payloads bloat the prompt until the SSE fetch dies before the first token."""
	if not isinstance(messages, list):
		return messages
	tool_names = {
		call.get("id"): (call.get("function") or {}).get("name")
		for message in messages
		if isinstance(message, dict) and message.get("role") == "assistant"
		for call in (message.get("tool_calls") or [])
		if isinstance(call, dict) and call.get("id")
	}
	tool_idxs = [
		i
		for i, message in enumerate(messages)
		if isinstance(message, dict) and message.get("role") == "tool"
	]
	keep = set(tool_idxs[-_TOOL_HISTORY_KEEP:])
	out: list[Any] = []
	for i, message in enumerate(messages):
		if not isinstance(message, dict) or message.get("role") != "tool":
			out.append(message)
			continue
		content = message.get("content") or ""
		if not isinstance(content, str):
			out.append(message)
			continue
		if tool_names.get(message.get("tool_call_id")) in _COMPLETE_TOOL_HISTORY_NAMES:
			out.append(message)
			continue
		slim = _slim_describe_json(content)
		limit = _TOOL_HISTORY_KEEP_LIMIT if i in keep else _TOOL_HISTORY_LIMIT
		content_out = slim if slim != content else content
		if len(content_out) > limit:
			content_out = content_out[:limit] + "\n…(truncated)"
		if content_out == content:
			out.append(message)
			continue
		trimmed = dict(message)
		trimmed["content"] = content_out
		out.append(trimmed)
	return out


def _fill_missing_tool_outputs(messages: Any) -> Any:
	"""OpenAI rejects history that has a function call without a matching tool result.

	That happens when a confirmation tool pauses, or a stream drops, after the
	assistant tool_calls were saved but before the tool row was written. Later
	turns then 400 immediately. Insert a stub result right after the unpaired call.
	"""
	if not isinstance(messages, list):
		return messages
	answered = {
		m.get("tool_call_id")
		for m in messages
		if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id")
	}
	out: list[Any] = []
	stub = json.dumps(
		{
			"status": "cancelled",
			"message": "The user continued without answering this tool confirmation. Do not retry this call.",
		}
	)
	for message in messages:
		out.append(message)
		if not isinstance(message, dict) or message.get("role") != "assistant":
			continue
		for call in message.get("tool_calls") or []:
			if not isinstance(call, dict):
				continue
			call_id = call.get("id")
			if not call_id or call_id in answered:
				continue
			out.append({"role": "tool", "tool_call_id": call_id, "content": stub})
			answered.add(call_id)
	return out


def install_model_compat() -> None:
	try:
		from flow.lib.model import Model
	except Exception:
		return
	if getattr(Model.chat, "_flow_tool_compact", False):
		return
	original = Model.chat

	def chat(self, messages, tools=None, *, stream=False):
		messages = _hide_images_from_model(
			_compact_tool_history(_fill_missing_tool_outputs(messages))
		)
		tools = _compact_tools(tools)
		if not stream:
			return original(self, messages, tools=tools, stream=False)
		saved_timeout = self.timeout
		saved_params = self.params
		self.timeout = max(int(self.timeout or 0), _STREAM_TIMEOUT)
		merged = dict(saved_params or {})
		merged["timeout"] = max(int(merged.get("timeout") or 0), _STREAM_TIMEOUT)
		merged["num_retries"] = 0
		self.params = merged
		try:
			return _heartbeat_stream(original(self, messages, tools=tools, stream=True))
		finally:
			self.timeout = saved_timeout
			self.params = saved_params

	chat._flow_tool_compact = True
	Model.chat = chat


def install_describe_compact() -> None:
	"""Shrink describe() before it hits SSE / the next model turn."""
	try:
		from flow.lib.agent import Agent
	except Exception:
		return
	if getattr(Agent._invoke, "_flow_describe_compact", False):
		return
	original = Agent._invoke

	def _invoke(self, call):
		result = original(self, call)
		if getattr(call, "name", None) == "describe":
			return _slim_describe(result)
		return result

	_invoke._flow_describe_compact = True
	Agent._invoke = _invoke


def _is_persist_conflict(err: BaseException) -> bool:
	name = type(err).__name__
	text = str(err)
	return (
		"TimestampMismatch" in name
		or "QueryDeadlock" in name
		or "1020" in text
		or "Record has changed" in text
		or "modified after you have opened" in text
	)


def _retry_on_conflict(fn, attempts: int = 5):
	last: BaseException | None = None
	for attempt in range(attempts):
		try:
			return fn(attempt)
		except Exception as err:
			last = err
			if not _is_persist_conflict(err) or attempt == attempts - 1:
				raise
			frappe.db.rollback()
			time.sleep(0.08 * (2 ** attempt))
	if last:
		raise last


def install_persist_retry() -> None:
	"""Session/run saves collide when two panels stream at once; retry instead of dropping the turn."""
	try:
		from flow.flow.doctype.flow_run import flow_run as fr_mod
		from flow.lib.agent import TextChunk
	except Exception:
		return
	if getattr(fr_mod.FlowRun.apply_result, "_flow_persist_retry", False):
		return

	original_apply = fr_mod.FlowRun.apply_result
	original_fail = fr_mod.FlowRun.mark_failed
	original_stream = fr_mod.stream_with_persistence

	def apply_result(self, result):
		def once(attempt):
			if attempt:
				self.reload()
			return original_apply(self, result)

		return _retry_on_conflict(once)

	def mark_failed(self, error):
		def once(attempt):
			if attempt:
				self.reload()
				if self.status in ("Completed", "Failed"):
					return
			return original_fail(self, error)

		return _retry_on_conflict(once, attempts=4)

	def stream_with_persistence(make_events, run):
		chunks: list[str] = []

		def wrapped():
			for event in make_events():
				if isinstance(event, TextChunk) and event.text:
					chunks.append(event.text)
				yield event

		try:
			yield from original_stream(wrapped, run)
		finally:
			_persist_interrupted_text(run, chunks)

	apply_result._flow_persist_retry = True
	fr_mod.FlowRun.apply_result = apply_result
	fr_mod.FlowRun.mark_failed = mark_failed
	fr_mod.stream_with_persistence = stream_with_persistence


def _persist_interrupted_text(run, chunks: list[str]) -> None:
	text = "".join(chunks).strip()
	if not text:
		return
	try:
		run.reload()
	except Exception:
		return
	if run.status not in ("Failed", "Running"):
		return
	if run.output and text in (run.output or ""):
		return

	def once(_attempt):
		session = frappe.get_doc("Flow Session", run.session)
		already = any(
			row.role == "assistant" and (row.content or "").strip() == text
			for row in session.messages
		)
		if already:
			return
		session.append("messages", {"role": "assistant", "content": text, "run": run.name})
		session.save(ignore_permissions=True)
		if not run.output:
			run.db_set("output", text[:5000], update_modified=False)
		if not frappe.flags.in_test:
			frappe.db.commit()

	try:
		_retry_on_conflict(once, attempts=4)
	except Exception:
		frappe.log_error(title="Flow interrupted persist failed")


def install_sse_flush() -> None:
	try:
		from flow.api import api as flow_api
		from werkzeug.wrappers import Response
	except Exception:
		return
	if getattr(flow_api._sse_response, "_flow_sse_flush", False):
		return
	original = flow_api._sse_response

	def _sse_response(events):
		resp = original(events)
		inner = resp.response
		headers = resp.headers
		headers["X-Accel-Buffering"] = "no"
		headers["Cache-Control"] = "no-cache"
		headers["Connection"] = "keep-alive"

		def body():
			# Pad past typical proxy buffers so the first frame actually reaches the browser.
			yield b":" + (b" " * 2048) + b"\n\n"
			yield from inner

		return Response(
			body(),
			status=resp.status,
			mimetype="text/event-stream",
			headers=headers,
		)

	_sse_response._flow_sse_flush = True
	flow_api._sse_response = _sse_response


_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")


def _strip_model_images(text: str) -> str:
	"""Drop model-written markdown images; only show_image tool URLs are trusted."""
	if not text or "![" not in text:
		return text
	return _MD_IMG_RE.sub("", text)


def _redact_show_image_payload(content: str) -> str:
	try:
		data = json.loads(content)
	except Exception:
		return content
	if not isinstance(data, dict) or "markdown" not in data or not data.get("url"):
		return content
	return json.dumps({"ok": True, "shown": True, "alt": data.get("alt") or ""}, ensure_ascii=False)


def _hide_images_from_model(messages: Any) -> Any:
	"""Copy history so the model cannot see prior image markdown/URLs and redraw them."""
	if not isinstance(messages, list):
		return messages
	out: list[Any] = []
	for message in messages:
		if not isinstance(message, dict):
			out.append(message)
			continue
		copied = dict(message)
		if copied.get("role") == "assistant" and isinstance(copied.get("content"), str):
			copied["content"] = _strip_model_images(copied["content"])
		if copied.get("role") == "tool" and isinstance(copied.get("content"), str):
			copied["content"] = _redact_show_image_payload(copied["content"])
		out.append(copied)
	return out


def _markdown_from_show_image(result: Any) -> str:
	if not result:
		return ""
	data = result
	if isinstance(result, str):
		try:
			data = json.loads(result)
		except Exception:
			return ""
	if not isinstance(data, dict):
		return ""
	markdown = (data.get("markdown") or "").strip()
	if markdown:
		match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", markdown)
		if not match:
			return markdown
		alt, url = match.groups()
		alt = alt.strip() or "image"
		if not alt.startswith("flowimg:"):
			alt = f"flowimg:{alt}"
		return f"![{alt}]({url})"
	url = (data.get("url") or "").strip()
	if not url:
		return ""
	alt = (data.get("alt") or "image").strip() or "image"
	if not alt.startswith("flowimg:"):
		alt = f"flowimg:{alt}"
	return f"![{alt}]({url})"


def _append_image_markdown(messages: list[Any] | None, markdown: str) -> None:
	if not markdown or not isinstance(messages, list):
		return
	url = ""
	if "](" in markdown:
		url = markdown.rsplit("](", 1)[-1].rstrip(")").strip()
	for message in reversed(messages):
		if not isinstance(message, dict) or message.get("role") != "assistant":
			continue
		content = message.get("content") or ""
		if markdown in content or (url and url in content):
			return
		message["content"] = (content.rstrip() + "\n\n" + markdown).strip()
		return


def _persist_show_image_markdowns(messages: list[Any] | None, markdowns: list[str]) -> None:
	"""Persist trusted show_image previews in the transcript that Flow stores."""
	for markdown in markdowns:
		_append_image_markdown(messages, markdown)


def install_show_image_inject() -> None:
	"""Stream show_image previews and persist them when the run is complete.

	The browser guard adds the preview to the live response after each tool event.
	Only the final RunResult is changed here, so tool-call and tool-result ordering
	stays valid while FlowSession can persist the preview for later reloads.
	"""
	try:
		from flow.lib.agent import Agent, Done, TextChunk, ToolEnded
	except Exception:
		return
	if getattr(Agent._loop_stream, "_flow_show_image", False):
		return
	original = Agent._loop_stream

	def _loop_stream(self, messages, executed_calls=None):
		image_markdowns: list[str] = []
		for event in original(self, messages, executed_calls):
			if isinstance(event, TextChunk) and event.text:
				cleaned = _strip_model_images(event.text)
				if cleaned != event.text:
					event = TextChunk(text=cleaned)
			if isinstance(event, ToolEnded) and event.name == "show_image":
				markdown = _markdown_from_show_image(event.result)
				if markdown:
					image_markdowns.append(markdown)
			elif isinstance(event, Done):
				_persist_show_image_markdowns(event.result.messages, image_markdowns)
			yield event

	_loop_stream._flow_show_image = True
	Agent._loop_stream = _loop_stream


class _RawEvent:
	"""JSON payload already shaped for SSE; skips Flow's dataclass conversion."""

	def __init__(self, payload: dict[str, Any]):
		self.payload = payload


class _KeepAlive:
	"""SSE comment frame; keeps proxies awake without counting as a Flow event."""


def _events_key(name: str) -> str:
	return f"flow-run-events|{name}"


def _alive_key(name: str) -> str:
	return f"flow-run-alive|{name}"


def _done_key(name: str) -> str:
	return f"flow-run-done|{name}"


def _touch_alive(name: str) -> None:
	frappe.cache().set_value(_alive_key(name), 1, expires_in_sec=90)


def _is_alive(name: str) -> bool:
	return bool(frappe.cache().get_value(_alive_key(name)))


def _mark_stream_done(name: str) -> None:
	frappe.cache().set_value(_done_key(name), 1, expires_in_sec=3600)


def _push_payload(name: str, payload: dict[str, Any]) -> None:
	cache = frappe.cache()
	key = _events_key(name)
	try:
		seq = int(cache.llen(key) or 0)
	except Exception:
		items = cache.get_value(key) or []
		seq = len(items) if isinstance(items, list) else 0
	payload = {**payload, "_seq": seq}
	raw = json.dumps(payload, default=str)
	try:
		cache.rpush(key, raw)
		cache.expire(key, 3600)
	except Exception:
		items = cache.get_value(key) or []
		if not isinstance(items, list):
			items = []
		items.append(raw)
		cache.set_value(key, items, expires_in_sec=3600)
	_touch_alive(name)


def _read_payloads(name: str, start: int) -> list[str]:
	cache = frappe.cache()
	key = _events_key(name)
	try:
		rows = cache.lrange(key, start, -1) or []
		out = []
		for row in rows:
			if isinstance(row, bytes):
				row = row.decode()
			elif not isinstance(row, str):
				row = json.dumps(row, default=str)
			out.append(row)
		return out
	except Exception:
		items = cache.get_value(key) or []
		if not isinstance(items, list):
			return []
		return [item if isinstance(item, str) else json.dumps(item, default=str) for item in items[start:]]


def _payload_from_event(event: Any) -> dict[str, Any]:
	from flow.api.api import _event_to_dict

	if isinstance(event, _RawEvent):
		return event.payload
	return _event_to_dict(event)


def _is_empty_text(payload: dict[str, Any]) -> bool:
	return payload.get("type") == "text" and not payload.get("delta")


def _terminal_from_run(run) -> dict[str, Any] | None:
	if run.status == "Failed":
		return {"type": "error", "message": run.error or "Run failed"}
	if run.status not in ("Completed", "Paused"):
		return None
	payload: dict[str, Any] = {
		"type": "done",
		"status": run.status,
		"iterations": run.iterations,
		"output": run.output,
		"usage": json.loads(run.usage) if run.usage else {},
	}
	if run.status == "Paused" and run.questions:
		payload["questions"] = json.loads(run.questions)
	return payload


def _iter_published(name: str, after: int = 0):
	import time

	last_text = None
	idx = max(int(after or 0), 0)
	idle = 0
	while True:
		rows = _read_payloads(name, idx)
		if rows:
			idle = 0
			for raw in rows:
				payload = json.loads(raw) if isinstance(raw, str) else raw
				idx += 1
				if not isinstance(payload, dict):
					continue
				if payload.get("type") == "text":
					delta = payload.get("delta") or ""
					if delta and delta == last_text:
						continue
					last_text = delta
				yield _RawEvent(payload)
				if payload.get("type") in ("done", "error"):
					return
			continue
		if frappe.cache().get_value(_done_key(name)):
			if idx == 0:
				run = frappe.get_doc("Flow Run", name)
				term = _terminal_from_run(run)
				if term:
					yield _RawEvent(term)
			return
		if idle == 0 or idle % 15 == 0:
			run = frappe.get_doc("Flow Run", name)
			if run.status != "Running":
				term = _terminal_from_run(run)
				if term:
					yield _RawEvent(term)
				return
		idle += 1
		yield _KeepAlive()
		time.sleep(1)
		if idle > 1800:
			yield _RawEvent({"type": "error", "message": "模型运行超时，请再发一次。"})
			return
		if idle > 45 and not _is_alive(name):
			run = frappe.get_doc("Flow Run", name)
			if run.status != "Running":
				term = _terminal_from_run(run)
				if term:
					yield _RawEvent(term)
				return
			yield _RawEvent({"type": "error", "message": "模型连接已中断，请再发一次。"})
			return


def _agent_worker(site: str, user: str, run_name: str, make_events) -> None:
	from flow.lib.agent import Done

	frappe.init(site=site)
	frappe.connect()
	frappe.set_user(user)
	frappe.flags.flow_run = run_name
	stop = threading.Event()

	def beat():
		while not stop.wait(20):
			_touch_alive(run_name)

	threading.Thread(target=beat, daemon=True, name=f"flow-alive-{run_name}").start()
	try:
		run = frappe.get_doc("Flow Run", run_name)
		last_text = None
		for event in make_events():
			payload = _payload_from_event(event)
			if _is_empty_text(payload):
				_touch_alive(run_name)
				continue
			if payload.get("type") == "text":
				delta = payload.get("delta") or ""
				if delta and delta == last_text:
					continue
				last_text = delta
			if isinstance(event, Done):
				run.apply_result(event.result)
				if not frappe.flags.in_test:
					frappe.db.commit()
				_push_payload(run_name, payload)
				_mark_stream_done(run_name)
				return
			_push_payload(run_name, payload)
		_mark_stream_done(run_name)
	except Exception as e:
		try:
			run = frappe.get_doc("Flow Run", run_name)
			run.mark_failed(str(e)[:5000])
			if not frappe.flags.in_test:
				frappe.db.commit()
			_push_payload(run_name, {"type": "error", "message": str(e)})
		except Exception:
			frappe.log_error(title="Flow background run failed")
		_mark_stream_done(run_name)
	finally:
		stop.set()
		frappe.flags.flow_run = None
		frappe.cache().delete_value(_alive_key(run_name))
		frappe.destroy()


def _detached_stream(make_events, run):
	from flow.flow.doctype.flow_run.flow_run import RunStarted

	started = RunStarted(name=run.name, session=run.session)
	_push_payload(run.name, _payload_from_event(started))
	_touch_alive(run.name)
	thread = threading.Thread(
		target=_agent_worker,
		args=(frappe.local.site, frappe.session.user, run.name, make_events),
		daemon=True,
		name=f"flow-run-{run.name}",
	)
	thread.start()
	try:
		yield from _iter_published(run.name, after=0)
	except GeneratorExit:
		return


def install_run_detach() -> None:
	"""Disabled. Replaying Redis events from after=0 on resume duplicated
	confirmations (approve once, same card appears again). Use official Flow streaming.
	"""
	return


install_model_compat()
install_sse_flush()
install_show_image_inject()
install_persist_retry()


def _mark_run_failed(name: str, error: str) -> None:
	run = frappe.get_doc("Flow Run", name)
	if run.status in ("Completed", "Failed"):
		return
	run.mark_failed(error)


@frappe.whitelist()
def unstick_session(session: str | None = None) -> dict[str, int]:
	"""Fail leftover Running runs so the user can send a new message."""
	if not isinstance(session, str) or not session.strip():
		frappe.throw(_("Session is required."), title=_("Invalid Session"))
	session = session.strip()

	from flow.lib.session import _assert_session_owner

	doc = frappe.get_doc("Flow Session", session)
	_assert_session_owner(doc)

	recovered = 0
	for name in frappe.get_all(
		"Flow Run",
		filters={"session": doc.name, "status": "Running"},
		pluck="name",
	):
		if _is_alive(name):
			continue
		_mark_run_failed(name, "Run abandoned: stream ended without completing.")
		recovered += 1
	return {"recovered": recovered}


def _scrub_missing_session_files(session: str) -> int:
	"""Drop chat attachment rows whose File was deleted, so session save can proceed."""
	removed = 0
	for row in frappe.get_all(
		"Flow Session Attachment",
		filters={"parent": session},
		fields=["name", "file"],
	):
		if not row.file or frappe.db.exists("File", row.file):
			continue
		frappe.db.delete("Flow Session Attachment", {"name": row.name})
		removed += 1
	return removed


@frappe.whitelist()
def recover_session(session: str | None = None) -> dict[str, int]:
	"""Same as official recover, but do not kill a run that is still executing."""
	return unstick_session(session)


@frappe.whitelist()
def live_run(session: str | None = None) -> dict[str, str | None]:
	"""Return the in-flight run for this session, if the worker is still alive."""
	if not isinstance(session, str) or not session.strip():
		frappe.throw(_("Session is required."), title=_("Invalid Session"))
	from flow.lib.session import _assert_session_owner

	doc = frappe.get_doc("Flow Session", session.strip())
	_assert_session_owner(doc)
	for name in frappe.get_all(
		"Flow Run",
		filters={"session": doc.name, "status": "Running"},
		pluck="name",
		order_by="creation desc",
	):
		if _is_alive(name) or _read_payloads(name, 0):
			return {"name": name}
	return {"name": None}


@frappe.whitelist()
def follow_run(run_name: str, after: int | str = 0, stream: bool | str = True):
	"""Reattach to a still-running (or just-finished) stream without starting a new turn."""
	from flow.api.api import _sse_response
	from flow.lib.session import assert_run_owner
	from frappe.utils import cint

	if not isinstance(run_name, str) or not run_name.strip():
		frappe.throw(_("Run is required."), title=_("Invalid Run"))
	run = frappe.get_doc("Flow Run", run_name.strip())
	assert_run_owner(run)
	start = cint(after)

	def events():
		yield from _iter_published(run.name, after=start)

	return _sse_response(events())


@frappe.whitelist()
def start_run(
	input: str,
	agent: str | None = None,
	session: str | None = None,
	model: str | None = None,
	attachments: list[str] | str | None = None,
	stream: bool | str = False,
):
	install_show_image_inject()
	install_describe_compact()
	install_persist_retry()
	if isinstance(session, str) and session.strip():
		try:
			unstick_session(session.strip())
		except Exception:
			frappe.log_error(title="Flow session unstick failed")
		try:
			_scrub_missing_session_files(session.strip())
		except Exception:
			frappe.log_error(title="Flow session attachment scrub failed")
	from flow.api.api import start_run as original

	return original(
		input=input,
		agent=agent,
		session=session,
		model=model,
		attachments=attachments,
		stream=stream,
	)


@frappe.whitelist()
def resume_run(
	run_name: str,
	answers: dict[str, Any] | str | None = None,
	stream: bool | str = False,
):
	install_show_image_inject()
	install_describe_compact()
	install_persist_retry()
	if not isinstance(run_name, str) or not run_name.strip():
		frappe.throw(_("Run is required."), title=_("Invalid Run"))
	from flow.api.api import resume_run as original

	return original(run_name=run_name, answers=answers, stream=stream)
