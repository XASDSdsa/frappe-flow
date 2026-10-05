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
	"""Show a picture in Flow chat using the exact attachment or document image.

	### Parameters and defaults

	- `file`: an exact File ID, an unambiguous file name, or a local URL.
	- For a document image, pass `doctype` and `name`; `field` defaults to `image`. This resolves that document's own attachment directly.
	- `alt`: optional short alt text; defaults to the file name, then `image`.

	### Results and next steps

	- Returns the preview URL, alt text, image Markdown, file name, and exact File ID.

	### Limits

	- If also passing `file` for a document image, it must match that document's image field.
	- A shared URL alone is ambiguous. Use the known document or exact File ID instead of retrying the same URL.
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
