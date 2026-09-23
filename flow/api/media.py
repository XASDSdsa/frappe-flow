"""Secure media helpers used by the Flow chat panel."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

import frappe
from frappe import _
from frappe.utils import get_url

_IMAGE_EXTENSIONS = {".apng", ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


def _looks_like_image(value: str) -> bool:
	path = (value or "").split("?", 1)[0].lower()
	return any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS)


def _file_url(file_url: str, is_private: int = 0, file_name: str | None = None) -> str:
	url = (file_url or "").strip()
	if url.startswith(("http://", "https://", "/")):
		return url
	name = (file_name or url).lstrip("/")
	return f"/private/files/{name}" if int(is_private or 0) else f"/files/{name}"


def _get_file(value: str):
	value = (value or "").strip()
	if not value:
		return None
	if frappe.db.exists("File", value):
		doc = frappe.get_doc("File", value)
		doc.check_permission("read")
		return doc
	for filters in ({"file_url": value}, {"file_name": value}):
		name = frappe.db.get_value("File", filters, "name")
		if name:
			doc = frappe.get_doc("File", name)
			doc.check_permission("read")
			return doc
	return None


def _local_file_url(value: str) -> str:
	parsed = urlsplit((value or "").strip())
	if parsed.scheme or parsed.netloc:
		if parsed.scheme not in {"http", "https"} or parsed.netloc != urlsplit(get_url()).netloc:
			frappe.throw(_("Only images stored on this site can be opened."))
	path = parsed.path
	if not path.startswith(("/files/", "/private/files/")):
		frappe.throw(_("Only images stored on this site can be opened."))
	return path


@frappe.whitelist()
def get_chat_original(file: str) -> dict[str, str]:
	"""Resolve a chat preview to its original File after checking read permission."""
	url = _local_file_url(file)
	doc = _get_file(url)
	if not doc and unquote(url) != url:
		doc = _get_file(unquote(url))
	if not doc:
		frappe.throw(_("Image attachment was not found."))
	if (doc.file_name or "").startswith("chat-preview-"):
		match = re.fullmatch(r"chat-preview-([a-zA-Z0-9]{10})([a-f0-9]{6})?\.jpg", doc.file_name)
		if not match or (match[2] and match[2] != (doc.get("content_hash") or "")[-6:]):
			frappe.throw(_("The original image for this preview could not be identified."))
		original_name = match[1]
		if not frappe.db.exists("File", original_name):
			frappe.throw(_("The original image attachment no longer exists."))
		original = frappe.get_doc("File", original_name)
		original.check_permission("read")
		if any((original.get(field) or "") != (doc.get(field) or "") for field in ("attached_to_doctype", "attached_to_name")):
			frappe.throw(_("The original image for this preview could not be identified."))
		doc = original
	url = _file_url(doc.file_url, doc.is_private, doc.file_name)
	if not _looks_like_image(doc.file_name or url):
		frappe.throw(_("This attachment is not an image."))
	return {"url": url, "file_name": doc.file_name}
