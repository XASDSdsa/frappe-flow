"""Offline contracts for signing, permission boundaries, settings and rate limits."""

import importlib.util
import inspect
import json
import base64
import hashlib
import hmac
import sys
import types
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_MODULE = "flow.flow.doctype.flow_voice_settings.flow_voice_settings"


class ValidationError(ValueError):
	pass


class TooManyRequestsError(Exception):
	pass


class Document(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__


def load_module(monkeypatch, name, relative_path):
	spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
	module = importlib.util.module_from_spec(spec)
	monkeypatch.setitem(sys.modules, name, module)
	spec.loader.exec_module(module)
	return module


@pytest.fixture
def env(monkeypatch):
	f = types.ModuleType("frappe")
	f._ = lambda text: text
	f.ValidationError = ValidationError
	f.PermissionError = PermissionError
	f.TooManyRequestsError = TooManyRequestsError
	f.session = types.SimpleNamespace(user="operator@example.com")
	f.local = types.SimpleNamespace(response_headers={})
	f.db = types.SimpleNamespace(get_value=Mock(return_value=Document(enabled=1, user_type="System User")))
	f.has_permission = Mock(return_value=True)
	f.get_roles = Mock(return_value=["All"])
	f.cache = types.SimpleNamespace(
		make_key=lambda key: f"site-a-db|{key}".encode(),
		eval=Mock(return_value=0),
	)

	def throw(message, exc=ValidationError):
		raise exc(message)

	def whitelist(**options):
		def decorate(fn):
			fn.whitelist_options = options
			return fn
		return decorate

	f.throw = throw
	f.whitelist = whitelist
	monkeypatch.setitem(sys.modules, "frappe", f)
	monkeypatch.setitem(sys.modules, "frappe.model", types.ModuleType("frappe.model"))
	document_module = types.ModuleType("frappe.model.document")
	document_module.Document = Document
	monkeypatch.setitem(sys.modules, "frappe.model.document", document_module)
	settings_module = load_module(monkeypatch, SETTINGS_MODULE,
		"flow/flow/doctype/flow_voice_settings/flow_voice_settings.py")
	backend = load_module(monkeypatch, "flow_voice_test_backend", "flow/flow_voice.py")
	settings = settings_module.FlowVoiceSettings(
		enabled=1, iflytek_app_id="1250000000", iflytek_api_key="APIKEYEXAMPLE", max_seconds=60, hotwords="",
	)
	settings.get_password = Mock(return_value="legacy-secret")
	f.get_single = Mock(return_value=settings)
	return types.SimpleNamespace(f=f, backend=backend, settings=settings, settings_module=settings_module)


def test_rtasr_signature_matches_official_formula_without_exposing_key(env):
	# RTASR signs hexadecimal MD5(appid + ts) with HMAC-SHA1(apiKey),
	# then base64 encodes the digest. Keep this independent of the backend.
	url = env.backend._signed_url(
		{"app_id": "1250000000", "api_key": "APIKEYEXAMPLE"}, timestamp=1700000000,
	)
	query = parse_qs(urlsplit(url).query)
	assert query["appid"] == ["1250000000"]
	assert query["ts"] == ["1700000000"]
	md5 = hashlib.md5(b"12500000001700000000").hexdigest()
	expected = base64.b64encode(hmac.new(b"APIKEYEXAMPLE", md5.encode(), hashlib.sha1).digest()).decode()
	assert query["signa"] == [expected]
	assert query["lang"] == ["cn"]
	assert "APIKEYEXAMPLE" not in url


def test_session_returns_fixed_endpoint_and_audio_contract(env):
	result = env.backend.create_session()
	assert set(result) == {"url", "provider", "app_id", "voice_id", "max_seconds", "sample_rate", "audio_format"}
	assert result["max_seconds"] == 60 and result["sample_rate"] == 16000
	assert result["audio_format"] == "pcm_s16le"
	url = urlsplit(result["url"])
	assert url.scheme == "wss" and url.netloc == "rtasr.xfyun.cn"
	assert url.path == "/v1/ws"
	query = parse_qs(url.query)
	assert query["appid"] == ["1250000000"]
	assert query["ts"] and query["signa"] and query["lang"] == ["cn"]
	assert "APIKEYEXAMPLE" not in result["url"]
	assert result["provider"] == "iflytek-rtasr" and result["app_id"] == "1250000000"
	assert env.f.local.response_headers["Cache-Control"] == "no-store"
	assert env.backend.create_session()["voice_id"] != result["voice_id"]


def test_native_http_restrictions_and_no_client_signing_arguments(env):
	assert env.backend.create_session.whitelist_options == {"methods": ["POST"]}
	assert env.backend.get_config.whitelist_options == {"methods": ["GET"]}
	assert not inspect.signature(env.backend.create_session).parameters
	with pytest.raises(TypeError):
		env.backend.create_session(app_id="999", max_seconds=999)


def test_config_is_safe_and_reports_admin_route_capability(env):
	assert env.backend.get_config() == {
		"enabled": True, "reason": "", "max_seconds": 60, "can_configure": False,
	}
	env.f.get_roles.return_value = ["All", "System Manager"]
	assert env.backend.get_config()["can_configure"] is True
	assert env.f.local.response_headers["Cache-Control"] == "no-store"
	env.f.has_permission.assert_called_with("Flow Session", ptype="create", user="operator@example.com")


@pytest.mark.parametrize("case", ["guest", "missing_user", "disabled", "portal", "no_flow_permission"])
def test_access_denials_never_read_settings_or_sign(env, case):
	if case == "guest":
		env.f.session.user = "Guest"
	elif case == "missing_user":
		env.f.db.get_value.return_value = None
	elif case == "disabled":
		env.f.db.get_value.return_value.enabled = 0
	elif case == "portal":
		env.f.db.get_value.return_value.user_type = "Website User"
	else:
		env.f.has_permission.return_value = False
	assert env.backend.get_config() == {
		"enabled": False, "reason": "forbidden", "max_seconds": 60, "can_configure": False,
	}
	with pytest.raises(PermissionError):
		env.backend.create_session()
	env.f.get_single.assert_not_called()
	env.f.cache.eval.assert_not_called()


@pytest.mark.parametrize("case,reason", [
	("disabled", "disabled"), ("key_missing", "not_configured"), ("key_blank", "not_configured"),
	("key_unreadable", "not_configured"), ("bad_id", "not_configured"),
	("bad_duration", "not_configured"), ("missing_doctype", "unavailable"),
])
def test_unconfigured_and_unavailable_status_is_safe(env, case, reason):
	if case == "disabled":
		env.settings.enabled = 0
	elif case == "key_missing":
		env.settings.iflytek_api_key = ""
	elif case == "key_blank":
		env.settings.iflytek_api_key = "   "
	elif case == "key_unreadable":
		env.settings.iflytek_api_key = "bad key!"
	elif case == "bad_id":
		env.settings.iflytek_app_id = "../arbitrary"
	elif case == "bad_duration":
		env.settings.max_seconds = 601
	else:
		env.f.get_single.side_effect = RuntimeError("sensitive detail")
	assert env.backend.get_config() == {
		"enabled": False, "reason": reason, "max_seconds": 60, "can_configure": False,
	}
	with pytest.raises(ValidationError, match="尚未启用或配置不完整") as error:
		env.backend.create_session()
	assert "sensitive detail" not in str(error.value)
	env.f.cache.eval.assert_not_called()


def test_rate_limit_uses_both_windows_and_site_user_namespaces(env):
	env.backend._enforce_session_limit()
	first = env.f.cache.eval.call_args.args
	assert first[1] == 2
	assert first[-4:] == (60, 10, 3600, 120)
	assert first[2].startswith(b"site-a-db|flow_voice:session:")
	assert first[2].endswith(b":60") and first[3].endswith(b":3600")
	assert b"operator@example.com" not in first[2]
	env.f.session.user = "other@example.com"
	env.backend._enforce_session_limit()
	assert env.f.cache.eval.call_args.args[2:4] != first[2:4]
	env.f.session.user = "operator@example.com"
	env.f.cache.make_key = lambda key: f"site-b-db|{key}".encode()
	env.backend._enforce_session_limit()
	assert env.f.cache.eval.call_args.args[2:4] != first[2:4]


@pytest.mark.parametrize("retry_after", [59, 3599])
def test_rate_limit_rejects_before_signing(env, monkeypatch, retry_after):
	env.f.cache.eval.return_value = retry_after
	build = Mock()
	monkeypatch.setattr(env.backend, "_session_details", build)
	with pytest.raises(TooManyRequestsError):
		env.backend.create_session()
	assert env.f.local.response_headers["Retry-After"] == str(retry_after)
	assert env.f.local.response_headers["Cache-Control"] == "no-store"
	build.assert_not_called()


def test_redis_failure_fails_closed_without_leaking_error(env, monkeypatch):
	env.f.cache.eval.side_effect = RuntimeError("private Redis address")
	build = Mock()
	monkeypatch.setattr(env.backend, "_session_details", build)
	with pytest.raises(ValidationError, match="暂时不可用") as error:
		env.backend.create_session()
	assert "private Redis address" not in str(error.value)
	build.assert_not_called()


def test_configuration_disabled_between_status_and_signing_returns_no_url(env, monkeypatch):
	def disable(*args):
		env.settings.enabled = 0
		return 0
	env.f.cache.eval.side_effect = disable
	with pytest.raises(ValidationError, match="暂时不可用"):
		env.backend.create_session()


def test_settings_may_be_saved_disabled_without_credentials(env):
	env.settings.update(enabled=0, iflytek_app_id="", iflytek_api_key="", max_seconds=None)
	env.settings.get_password.return_value = None
	env.settings.validate()
	assert env.settings.max_seconds == 60
	env.settings.get_password.assert_not_called()


@pytest.mark.parametrize("field,value", [
	("iflytek_app_id", ""), ("iflytek_api_key", ""), ("iflytek_app_id", "123/../../"),
	("iflytek_app_id", "１２３４"), ("iflytek_app_id", "a" * 65), ("iflytek_api_key", "id&nonce=1"),
	("max_seconds", 0), ("max_seconds", 601),
	("max_seconds", 1.5), ("max_seconds", True), ("max_seconds", "no"),
])
def test_settings_reject_invalid_values(env, field, value):
	env.settings[field] = value
	with pytest.raises(ValidationError):
		env.settings.validate()


def test_settings_do_not_require_legacy_secret_for_rtasr(env):
	env.settings.get_password.return_value = None
	env.settings.validate()


@pytest.mark.parametrize("word", ["too long " + "x" * 30, "x" * 31, "台" * 11,
	"two words", "tab\tword", "a,b", "a|100", " leading", "trailing ", "a\x00b", "a\u200bb"])
def test_hotwords_reject_bad_terms(env, word):
	with pytest.raises(ValueError):
		env.settings_module.normalize_hotwords(word)


def test_hotwords_are_generic_plus_explicit_lines_deduplicated_and_weight_five(env):
	result = env.settings_module.normalize_hotwords("台球\nCue+Pro\n\n贴纸\nCue+Pro\n" + "台" * 10 + "x" * 20)
	words = result.split(",")
	assert len(words) == len(env.settings_module.DEFAULT_HOTWORDS) + 2
	assert len(words) == len(set(words))
	assert all(word.endswith("|5") for word in words)
	assert "Cue+Pro|5" in words
	assert "台球|5" in words and "贴纸|5" in words


def test_hotword_limit_includes_builtin_terms(env):
	count = 128 - len(env.settings_module.DEFAULT_HOTWORDS)
	words = [f"term{i}" for i in range(count)]
	assert len(env.settings_module.normalize_hotwords("\n".join(words)).split(",")) == 128
	with pytest.raises(ValueError, match="128"):
		env.settings_module.normalize_hotwords("\n".join(words + ["extra"]))


def test_doctype_exposes_credentials_only_to_system_manager():
	doc = json.loads((ROOT / "flow/flow/doctype/flow_voice_settings/flow_voice_settings.json").read_text())
	assert doc["issingle"] == 1 and doc["module"] == "Flow"
	assert doc["permissions"] == [{"create": 1, "read": 1, "role": "System Manager", "write": 1}]
	fields = {row["fieldname"]: row for row in doc["fields"]}
	assert fields["enabled"]["default"] == "0"
	assert fields["iflytek_api_secret"]["fieldtype"] == "Password"
	assert fields["iflytek_api_secret"]["hidden"] == 1
	assert fields["iflytek_api_secret"]["read_only"] == 1
	assert fields["max_seconds"]["default"] == "60"
	assert doc["track_changes"] == 0
