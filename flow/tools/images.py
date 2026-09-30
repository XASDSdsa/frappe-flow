"""Generic, permission-checked image display for Flow chat."""

from __future__ import annotations

from typing import Annotated

import frappe
from frappe import _

from flow.api.media import _ensure_chat_preview, _get_file, _image_file_from_doc, _looks_like_image
from flow.lib.tool import tool


@tool
def show_image(
	file: Annotated[str | None, "Exact File ID, or an unambiguous file name or local URL"] = None,
	doctype: Annotated[str | None, "DocType that has the image, e.g. Item"] = None,
	name: Annotated[str | None, "Document name"] = None,
	field: Annotated[str | None, "Image fieldname, default image"] = None,
	alt: Annotated[str | None, "Short alt text for the markdown image"] = None,
) -> dict:
	"""Show a picture in Flow chat. For a document image, pass doctype, name and field
	(default image). This resolves that document's own attachment directly. If also
	passing file, it must match that field. A shared URL alone is ambiguous: use the
	known document or exact File ID instead of retrying the same URL.
	"""
	doctype, name, field = ((value or "").strip() for value in (doctype, name, field))
	if doctype or name or field:
		if not (doctype and name):
			frappe.throw(_("Both DocType and document name are required for a document image."))
		doc = _image_file_from_doc(doctype, name, field or None, file)
	elif file:
		doc = _get_file(file)
	else:
		frappe.throw(_("Pass file, or doctype plus name."))
	if not doc:
		frappe.throw(_("No image found."))
	if not _looks_like_image(doc.file_name or doc.file_url):
		frappe.throw(_("This attachment is not an image."))
	url = _ensure_chat_preview(doc)
	file_name = doc.file_name
	label = (alt or file_name or "image").strip() or "image"
	return {
		"ok": True,
		"url": url,
		"alt": label,
		"markdown": f"![{label}]({url})",
		"file_name": file_name,
		"file_id": doc.name,
	}
