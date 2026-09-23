"""Authenticated iFlytek RTASR streaming sessions for the Flow composer."""

import base64
import hashlib
import hmac
import time
import uuid
from urllib.parse import urlencode

import frappe
from frappe import _

from flow.flow.doctype.flow_voice_settings.flow_voice_settings import (
	DEFAULT_MAX_SECONDS,
	normalize_settings,
)

SETTINGS_DOCTYPE = "Flow Voice Settings"
ASR_HOST = "rtasr.xfyun.cn"
ASR_PATH = "/v1/ws"
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


def _signed_url(config, *, timestamp=None):
	"""Build the RTASR URL using the product's appid+ts HMAC-SHA1 signature."""
	ts = str(int(timestamp if timestamp is not None else time.time()))
	md5 = hashlib.md5(f'{config["app_id"]}{ts}'.encode("utf-8")).hexdigest()
	signa = base64.b64encode(
		hmac.new(config["api_key"].encode("utf-8"), md5.encode("utf-8"), hashlib.sha1).digest()
	).decode("ascii")
	query = urlencode({"appid": config["app_id"], "ts": ts, "signa": signa, "lang": "cn"})
	return f"wss://{ASR_HOST}{ASR_PATH}?{query}"


def _session_details():
	"""Keep credential-bearing locals outside any public exception traceback."""
	try:
		settings = frappe.get_single(SETTINGS_DOCTYPE)
		if not settings.enabled:
			return None
		config = normalize_settings(settings)
		voice_id = str(uuid.uuid4())
		return {
			"url": _signed_url(config),
			"provider": "iflytek-rtasr",
			"app_id": config["app_id"],
			"voice_id": voice_id,
			"max_seconds": config["max_seconds"],
			"sample_rate": 16000,
			"audio_format": "pcm_s16le",
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
