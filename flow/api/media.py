"""Secure media helpers used by the Flow chat panel."""

from __future__ import annotations

import re
from os.path import splitext
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import frappe
from frappe import _
from frappe.utils import get_url

_IMAGE_EXTENSIONS = {".apng", ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
_PREVIEW_MAX_PX = 720


def _looks_like_image(value: str) -> bool:
	path = (value or "").split("?", 1)[0].lower()
	return any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS)


def _file_url(file_url: str, is_private: int = 0, file_name: str | None = None) -> str:
	url = (file_url or "").strip()
	if url.startswith(("http://", "https://", "/")):
		return url
	name = (file_name or url).lstrip("/")
	return f"/private/files/{name}" if int(is_private or 0) else f"/files/{name}"


def _ensure_chat_preview(doc) -> str:
	"""Return a small same-permission JPEG for chat, keeping originals for click-to-open."""
	url = _image_url(doc)
	ext = splitext((doc.file_name or doc.file_url or "").split("?", 1)[0])[1].lower()
	if (doc.file_name or "").startswith("chat-preview-") and ext in {".jpg", ".jpeg"}:
		return url
	prefix = f"chat-preview-{doc.name}"
	existing = frappe.get_all(
		"File",
		filters={
			"file_name": ("like", prefix + "%"),
			"attached_to_doctype": doc.attached_to_doctype,
			"attached_to_name": doc.attached_to_name,
			"is_private": int(doc.is_private or 0),
		},
		pluck="name",
		limit_page_length=2,
	)
	if len(existing) > 1:
		frappe.throw(_("Multiple image previews match this attachment. The attachment needs review."))
	if existing:
		preview = _read_image_file(existing[0])
		match = re.fullmatch(re.escape(prefix) + r"([a-f0-9]{6})?\.jpg", preview.file_name or "")
		if not match or (match[1] and match[1] != (preview.get("content_hash") or "")[-6:]):
			frappe.throw(_("The original image for this preview could not be identified."))
		return _image_url(preview)
	try:
		from io import BytesIO

		from PIL import Image

		image = Image.open(BytesIO(doc.get_content()))
		if image.mode != "RGB":
			image = image.convert("RGB")
		image.thumbnail((_PREVIEW_MAX_PX, _PREVIEW_MAX_PX))
		buf = BytesIO()
		image.save(buf, format="JPEG", quality=72, optimize=True)
		# File.insert reuses identical bytes without replacing our logical filename.
		# file_manager.save_file also copies the old filename, losing the original ID.
		saved = frappe.get_doc({
			"doctype": "File",
			"file_name": f"{prefix}.jpg",
			"content": buf.getvalue(),
			"attached_to_doctype": doc.attached_to_doctype,
			"attached_to_name": doc.attached_to_name,
			"is_private": int(doc.is_private or 0),
		}).insert(ignore_permissions=True)
		return _image_url(saved)
	except Exception:
		frappe.log_error(title="Flow chat image preview failed")
		return url


def _image_url(doc) -> str:
	"""Keep native File identity even when content deduplication shares a URL."""
	url = _file_url(doc.file_url, doc.is_private, doc.file_name)
	parts = urlsplit(url)
	query = parse_qs(parts.query, keep_blank_values=True)
	query["fid"] = [doc.name]
	return parts._replace(query=urlencode(query, doseq=True)).geturl()


def _check_image_read(doc):
	try:
		doc.check_permission("read")
	except frappe.PermissionError:
		frappe.throw(_("You do not have permission to read this image or its document."), frappe.PermissionError)
	return doc


def _read_image_file(name: str):
	return _check_image_read(frappe.get_doc("File", name))


def _get_file(value: str, *, doctype: str | None = None, name: str | None = None, field: str | None = None):
	"""Resolve an exact File ID or an unambiguous, optionally document-scoped reference.

	A file_url is not a File identity: native integrations reuse its bytes through
	separate attachment records. Never let another document's record win by row order.
	"""
	value = (value or "").strip()
	if not value:
		return None
	scope = {}
	if doctype and name:
		scope = {"attached_to_doctype": doctype, "attached_to_name": name}
		if field:
			scope["attached_to_field"] = field
	is_url = value.startswith(("/", "http://", "https://"))
	if is_url:
		path = _local_file_url(value)
		query = parse_qs(urlsplit(value).query, keep_blank_values=True)
		if "fid" in query:
			ids = query["fid"]
			if len(ids) != 1 or not ids[0] or not frappe.db.exists("File", ids[0]):
				frappe.throw(_("Image attachment was not found."))
			doc = _get_file(ids[0], doctype=doctype, name=name, field=field)
			if unquote(_local_file_url(doc.file_url)) != unquote(path):
				frappe.throw(_("The supplied file does not match the image in the requested document field."))
			return doc
	if not is_url and frappe.db.exists("File", value):
		doc = _read_image_file(value)
		if any(doc.get(key) != expected for key, expected in scope.items()):
			frappe.throw(_("The image attachment does not belong to the requested document field."))
		return doc
	if is_url:
		filters = {"file_url": _local_file_url(value), **scope}
	else:
		filters = {"file_name": value, **scope}
	matches = frappe.get_all("File", filters=filters, pluck="name", limit_page_length=2)
	if len(matches) > 1:
		frappe.throw(_("Multiple image attachments match. Specify the document and image field, or an exact File ID."))
	return _read_image_file(matches[0]) if matches else None


def _local_file_url(value: str) -> str:
	parsed = urlsplit((value or "").strip())
	if parsed.scheme or parsed.netloc:
		if parsed.scheme not in {"http", "https"} or parsed.netloc != urlsplit(get_url()).netloc:
			frappe.throw(_("Only images stored on this site can be opened."))
	path = parsed.path
	if not path.startswith(("/files/", "/private/files/")):
		frappe.throw(_("Only images stored on this site can be opened."))
	return path


def _image_file_from_doc(doctype: str, name: str, field: str | None = None, file: str | None = None):
	"""Keep the document/field identity through lookup, preview and permission checks."""
	doc = _check_image_read(frappe.get_doc(doctype, name))
	field = (field or "image").strip()
	if not doc.meta.has_field(field):
		frappe.throw(_("Image field {0} does not exist on {1}.").format(field, doctype))
	value = str(doc.get(field) or "").strip()
	if not value:
		frappe.throw(_("No image is set in the requested document field."))
	attachment = _get_file(file or value, doctype=doctype, name=name, field=field)
	if not attachment:
		frappe.throw(_("Image attachment was not found for the requested document field."))
	if _local_file_url(attachment.file_url) != _local_file_url(value):
		frappe.throw(_("The supplied file does not match the image in the requested document field."))
	return attachment


@frappe.whitelist()
def get_chat_original(file: str) -> dict[str, str]:
	"""Resolve a chat preview to its original File after checking read permission."""
	url = _local_file_url(file)
	doc = _get_file(file)
	if not doc and unquote(url) != url:
		doc = _get_file(unquote(file))
	if not doc:
		frappe.throw(_("Image attachment was not found."))
	if (doc.file_name or "").startswith("chat-preview-"):
		match = re.fullmatch(r"chat-preview-([a-zA-Z0-9]{10})([a-f0-9]{6})?\.jpg", doc.file_name)
		if not match or (match[2] and match[2] != (doc.get("content_hash") or "")[-6:]):
			frappe.throw(_("The original image for this preview could not be identified."))
		original_name = match[1]
		if not frappe.db.exists("File", original_name):
			frappe.throw(_("The original image attachment no longer exists."))
		original = _read_image_file(original_name)
		if (
			any((original.get(field) or "") != (doc.get(field) or "") for field in ("attached_to_doctype", "attached_to_name"))
			or bool(original.is_private) != bool(doc.is_private)
		):
			frappe.throw(_("The original image for this preview could not be identified."))
		doc = original
	url = _image_url(doc)
	if not _looks_like_image(doc.file_name or url):
		frappe.throw(_("This attachment is not an image."))
	return {"url": url, "file_name": doc.file_name}
