"""Authenticated, short-lived iFlytek streaming ASR sessions for the Flow composer."""

import base64
import email.utils
import hashlib
import hmac
import time
import uuid
from urllib.parse import quote, urlencode

import frappe
from frappe import _

from flow.flow.doctype.flow_voice_settings.flow_voice_settings import (
	DEFAULT_MAX_SECONDS,
	normalize_settings,
)

SETTINGS_DOCTYPE = "Flow Voice Settings"
ASR_HOST = "iat-api.xfyun.cn"
ASR_PATH = "/v2/iat"
SESSION_LIMITS = ((60, 10), (3600, 120))

# RedisWrapper.eval is inherited from redis.Redis; make_key supplies the site's
# database prefix. INCR and first-use EXPIRE must run together, even on crashes.
_LIMIT_SCRIPT = """
local retry_after = 0
for i, key in ipairs(KEYS) do
    local count = redis.call('INCR', key)
    local seconds = tonumber(ARGV[2 * i - 1])
    if count == 1 then redis.call('EXPIRE', key, seconds) end
    if count > tonumber(ARGV[2 * i]) then
        retry_after = math.max(retry_after, redis.call('TTL', key))
    end
end
return retry_after
"""


def _no_store():
	frappe.local.response_headers["Cache-Control"] = "no-store"


def _has_access():
	user = frappe.session.user
	if not user or user == "Guest":
		return False
	account = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
	return bool(
		account
		and account.enabled
		and account.user_type == "System User"
		and frappe.has_permission("Flow Session", ptype="create", user=user)
	)


def _configuration_state():
	"""Return only public status, keeping decryption errors and credentials private."""
	state = {"enabled": False, "reason": "disabled", "max_seconds": DEFAULT_MAX_SECONDS}
	try:
		settings = frappe.get_single(SETTINGS_DOCTYPE)
		if not settings.enabled:
			return state
		try:
			config = normalize_settings(settings)
			if not (settings.get_password("iflytek_api_secret", raise_exception=False) or "").strip():
				state["reason"] = "not_configured"
				return state
		except Exception:
			state["reason"] = "not_configured"
			return state
		state.update(enabled=True, reason="", max_seconds=config["max_seconds"])
	except Exception:
		state["reason"] = "unavailable"
	return state


@frappe.whitelist(methods=["GET"])
def get_config():
	"""No credentials, hotwords, signed URLs, or business records are returned."""
	_no_store()
	if not _has_access():
		return {
			"enabled": False,
			"reason": "forbidden",
			"max_seconds": DEFAULT_MAX_SECONDS,
			"can_configure": False,
		}
	return {
		**_configuration_state(),
		"can_configure": "System Manager" in frappe.get_roles(),
	}


def _enforce_session_limit():
	identity = hashlib.sha256(frappe.session.user.encode("utf-8")).hexdigest()
	cache = frappe.cache
	keys = [cache.make_key(f"flow_voice:session:{identity}:{seconds}") for seconds, _ in SESSION_LIMITS]
	arguments = [value for limit in SESSION_LIMITS for value in limit]
	try:
		retry_after = int(cache.eval(_LIMIT_SCRIPT, len(keys), *keys, *arguments))
	except Exception:
		# Fail closed if Redis is down; never issue unlimited paid sessions.
		frappe.throw(_("语音服务暂时不可用，请稍后重试。"))
	if retry_after:
		frappe.local.response_headers["Retry-After"] = str(retry_after)
		frappe.throw(_("语音使用过于频繁，请稍后重试。"), frappe.TooManyRequestsError)


def _signed_url(config, api_secret, *, timestamp=None):
	date = email.utils.formatdate(timestamp or time.time(), usegmt=True)
	request_line = f"GET {ASR_PATH} HTTP/1.1"
	signature_origin = f"host: {ASR_HOST}\ndate: {date}\n{request_line}"
	signature = base64.b64encode(
		hmac.new(api_secret.encode("utf-8"), signature_origin.encode("utf-8"), hashlib.sha256).digest()
	).decode("ascii")
	authorization_origin = (
		f'api_key="{config["api_key"]}", algorithm="hmac-sha256", '
		f'headers="host date request-line", signature="{signature}"'
	)
	authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("ascii")
	query = urlencode({"authorization": authorization, "date": date, "host": ASR_HOST}, quote_via=quote)
	return f"wss://{ASR_HOST}{ASR_PATH}?{query}"


def _session_details():
	"""Keep credential-bearing locals outside any public exception traceback."""
	try:
		settings = frappe.get_single(SETTINGS_DOCTYPE)
		if not settings.enabled:
			return None
		config = normalize_settings(settings)
		api_secret = settings.get_password("iflytek_api_secret", raise_exception=False)
		if not api_secret or not api_secret.strip():
			return None
		voice_id = str(uuid.uuid4())
		return {
			"url": _signed_url(config, api_secret),
			"provider": "iflytek",
			"app_id": config["app_id"],
			"voice_id": voice_id,
			"max_seconds": config["max_seconds"],
			"sample_rate": 16000,
		}
	except Exception:
		return None


@frappe.whitelist(methods=["POST"])
def create_session():
	"""Issue a fixed-purpose URL; no client signing parameters are accepted."""
	_no_store()
	if not _has_access():
		frappe.throw(_("没有使用 Flow 语音输入的权限。"), frappe.PermissionError)
	if not _configuration_state()["enabled"]:
		frappe.throw(_("语音输入尚未启用或配置不完整，请联系系统管理员。"))
	_enforce_session_limit()
	result = _session_details()
	if result is None:
		frappe.throw(_("语音服务暂时不可用，请联系系统管理员。"))
	return result
