"""Generic, permission-checked image display for Flow chat."""

from __future__ import annotations

from typing import Annotated

import frappe
from frappe import _

from flow.api.media import _display_url, _ensure_chat_preview, _get_file, _image_from_doc
from flow.lib.tool import tool


@tool
def show_image(
	file: Annotated[str | None, "File name or /files/... URL"] = None,
	doctype: Annotated[str | None, "DocType that has the image, e.g. Item"] = None,
	name: Annotated[str | None, "Document name"] = None,
	field: Annotated[str | None, "Image fieldname, default image"] = None,
	alt: Annotated[str | None, "Short alt text for the markdown image"] = None,
) -> dict:
	"""Show a permission-checked picture in Flow chat; the panel renders it automatically."""
	url = None
	file_name = None
	if file:
		doc = _get_file(file)
		if doc:
			url = _ensure_chat_preview(doc)
			file_name = doc.file_name
		else:
			url, file_name = _display_url(str(file).strip())
	elif doctype and name:
		url, file_name = _display_url(_image_from_doc(doctype.strip(), name.strip(), field))
	else:
		frappe.throw(_("Pass file, or doctype plus name."))
	if not url:
		frappe.throw(_("No image found."))
	label = (alt or file_name or "image").strip() or "image"
	return {
		"ok": True,
		"url": url,
		"alt": label,
		"markdown": f"![{label}]({url})",
		"file_name": file_name,
	}
