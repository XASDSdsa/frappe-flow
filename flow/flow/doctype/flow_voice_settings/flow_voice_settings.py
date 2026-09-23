"""Administrator-owned iFlytek voice settings; hotwords never query business records."""

import re
import unicodedata

import frappe
from frappe import _
from frappe.model.document import Document

DEFAULT_MAX_SECONDS = 60
# RTASR itself supports long-lived streams. Flow still keeps a configurable
# safety cap so a forgotten microphone cannot create an unbounded paid session.
MAX_SECONDS = 600
DEFAULT_HOTWORDS = ("台球", "球杆", "皮头", "先角", "巧粉", "贴纸", "标签")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")


def normalize_hotwords(value):
	words = list(DEFAULT_HOTWORDS)
	for line in (value or "").splitlines():
		if not line:
			continue
		if (
			len(line) > 30
			or len(_HAN.findall(line)) > 10
			or any(char.isspace() or unicodedata.category(char).startswith("C") for char in line)
			or "," in line
			or "|" in line
		):
			raise ValueError("每个热词最多 30 个字符、10 个汉字，不能包含空白、逗号或竖线。")
		if line not in words:
			words.append(line)
	if len(words) > 128:
		raise ValueError("自定义热词与内置热词合计不能超过 128 个。")
	return ",".join(f"{word}|5" for word in words)


def normalize_settings(settings):
	app_id = str(settings.get("iflytek_app_id") or "").strip()
	api_key = str(settings.get("iflytek_api_key") or "").strip()
	if app_id and not re.fullmatch(r"[A-Za-z0-9]{1,64}", app_id):
		raise ValueError("科大讯飞 AppID 只能包含英文字母和数字，最多 64 位。")
	if api_key and not re.fullmatch(r"[A-Za-z0-9]{1,128}", api_key):
		raise ValueError("科大讯飞 APIKey 只能包含英文字母和数字，最多 128 位。")
	seconds = settings.get("max_seconds")
	if seconds is None or seconds == "":
		seconds = DEFAULT_MAX_SECONDS
	if not re.fullmatch(r"[0-9]+", str(seconds)) or not 1 <= int(seconds) <= MAX_SECONDS:
		raise ValueError(f"单次录音时长必须为 1 到 {MAX_SECONDS} 秒的整数。")
	if settings.get("enabled") and (not app_id or not api_key):
		raise ValueError("启用语音输入前，请填写科大讯飞实时转写 AppID 和 APIKey。")
	return {
		"app_id": app_id,
		"api_key": api_key,
		"max_seconds": int(seconds),
		"hotwords": settings.get("hotwords") or "",
	}


class FlowVoiceSettings(Document):
	def validate(self):
		try:
			config = normalize_settings(self)
		except ValueError as error:
			frappe.throw(_(str(error)))
		self.iflytek_app_id = config["app_id"]
		self.iflytek_api_key = config["api_key"]
		self.max_seconds = config["max_seconds"]
