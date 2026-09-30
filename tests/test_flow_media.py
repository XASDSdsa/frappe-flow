"""Offline regressions for attachment identity and image permission boundaries."""

import hashlib
import importlib.util
import sys
import types
from fnmatch import fnmatchcase
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
IMAGE_URL = "/private/files/shared-product.png"


class ValidationError(ValueError):
	pass


class Document(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__

	def check_permission(self, permission):
		assert permission == "read"
		self.permission_checks.append(permission)
		if not self.readable:
			# Native File permission failures may have no message. The media API must
			# still give callers a useful error without trying a different File.
			raise PermissionError

	def has_permission(self, permission="read", **_kwargs):
		assert permission == "read"
		return self.readable

	def get_content(self):
		if self.get("content"):
			return self.content
		buffer = BytesIO()
		Image.new("RGB", (2, 2), "white").save(buffer, "PNG")
		return buffer.getvalue()


class FileDocument(Document):
	def __init__(self, store, **values):
		super().__init__(**values)
		object.__setattr__(self, "_store", store)

	@property
	def unique_url(self):
		if self.is_private:
			return self.file_url + "?" + urlencode({"fid": self.name})
		return self.file_url

	def insert(self, **kwargs):
		return self._store.insert_file(self, **kwargs)


class Store:
	def __init__(self):
		self.docs = {}
		self.metas = {}
		self.inserted = []

	def add_document(self, doctype="Item", name="ITEM-001", image=IMAGE_URL, readable=True, **values):
		doc = Document(
			doctype=doctype, name=name, image=image, readable=readable, permission_checks=[], **values,
		)
		self.docs[(doctype, name)] = doc
		fields = {key: Document(fieldname=key, fieldtype="Attach Image") for key in ["image", *values]}
		self.metas[doctype] = types.SimpleNamespace(
			image_field="image", has_field=lambda key: key in fields, get_field=fields.get,
		)
		doc.meta = self.metas[doctype]
		return doc

	def add_file(self, name="itemfile01", file_url=IMAGE_URL, file_name="shared-product.png",
		attached_to_doctype="Item", attached_to_name="ITEM-001", attached_to_field="image",
		is_private=1, readable=True, **values):
		doc = FileDocument(
			self,
			doctype="File", name=name, file_url=file_url, file_name=file_name,
			attached_to_doctype=attached_to_doctype, attached_to_name=attached_to_name,
			attached_to_field=attached_to_field, is_private=is_private, readable=readable,
			permission_checks=[], **values,
		)
		self.docs[("File", name)] = doc
		return doc

	def get_doc(self, doctype, name=None):
		if isinstance(doctype, dict):
			assert doctype["doctype"] == "File"
			return FileDocument(self, readable=True, permission_checks=[], **doctype)
		return self.docs[(doctype, name)]

	def insert_file(self, doc, ignore_permissions=False):
		assert ignore_permissions is True
		assert isinstance(doc.content, bytes) and doc.content
		content_hash = hashlib.md5(doc.content).hexdigest()
		matches = self.select("File", {"content_hash": content_hash, "is_private": doc.is_private})
		url_prefix = "/private/files" if doc.is_private else "/files"
		# Native File content deduplication reuses bytes/URL, preserving the new
		# document's requested file_name and attachment identity.
		doc.file_url = matches[0].file_url if matches else f"{url_prefix}/{doc.file_name}"
		doc.name = f"created{len(self.inserted) + 1:03}"
		doc.content_hash = content_hash
		self.docs[("File", doc.name)] = doc
		self.inserted.append(doc)
		return doc

	def select(self, doctype, filters=None):
		if isinstance(filters, str):
			filters = {"name": filters}

		def matches(doc):
			for key, expected in (filters or {}).items():
				actual = doc.get(key)
				if isinstance(expected, (tuple, list)):
					operator, value = expected
					if operator == "like":
						if not fnmatchcase(actual or "", value.replace("%", "*")):
							return False
					elif operator == "in":
						if actual not in value:
							return False
					else:
						raise AssertionError(f"Unsupported test filter: {expected!r}")
				elif actual != expected:
					return False
			return True

		return [doc for (kind, _name), doc in self.docs.items() if kind == doctype and matches(doc)]

	def get_all(self, doctype, filters=None, fields=None, pluck=None, limit=None, limit_page_length=None,
		order_by=None, **_kwargs):
		rows = self.select(doctype, filters)
		rows = rows[:limit or limit_page_length or len(rows)]
		if pluck:
			return [row.get(pluck) for row in rows]
		return [Document({key: row.get(key) for key in (fields or ["name"])}) for row in rows]

	def get_value(self, doctype, filters, fieldname="name", **_kwargs):
		rows = self.select(doctype, filters)
		if not rows:
			return None
		return rows[0].get(fieldname)

	def exists(self, doctype, filters):
		return self.get_value(doctype, filters)


def load_module(monkeypatch, name, relative_path):
	spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
	module = importlib.util.module_from_spec(spec)
	monkeypatch.setitem(sys.modules, name, module)
	spec.loader.exec_module(module)
	return module


@pytest.fixture
def env(monkeypatch):
	store = Store()
	f = types.ModuleType("frappe")
	f._ = lambda value: value
	f.PermissionError = PermissionError
	f.ValidationError = ValidationError
	f.whitelist = lambda **_kwargs: lambda function: function
	f.get_doc = Mock(side_effect=store.get_doc)
	f.get_meta = Mock(side_effect=store.metas.get)
	f.get_all = Mock(side_effect=store.get_all)
	f.db = types.SimpleNamespace(exists=store.exists, get_value=store.get_value)
	f.log_error = Mock()

	def throw(message, exc=ValidationError, **_kwargs):
		raise exc(message)

	f.throw = throw
	utils = types.ModuleType("frappe.utils")
	utils.get_url = lambda: "https://erp.example.test"
	file_manager = types.ModuleType("frappe.utils.file_manager")

	def save_file(file_name, content, doctype, name, is_private=0, **kwargs):
		assert content
		matches = store.select("File", {"content_hash": hashlib.md5(content).hexdigest(), "is_private": is_private})
		return store.get_doc({
			"doctype": "File", "content": content,
			"file_name": matches[0].file_name if matches else file_name,
			"attached_to_doctype": doctype, "attached_to_name": name,
			"attached_to_field": kwargs.get("df"), "is_private": is_private,
		}).insert(ignore_permissions=True)

	file_manager.save_file = Mock(side_effect=save_file)
	tool = types.ModuleType("flow.lib.tool")
	tool.tool = lambda function: function
	for module in (f, utils, file_manager, tool):
		monkeypatch.setitem(sys.modules, module.__name__, module)
	media = load_module(monkeypatch, "flow.api.media", "flow/api/media.py")
	images = load_module(monkeypatch, "flow_images_under_test", "flow/tools/images.py")
	ensure_preview = media._ensure_chat_preview
	preview = Mock(side_effect=lambda doc: f"/private/files/chat-preview-{doc.name}.jpg")
	monkeypatch.setattr(media, "_ensure_chat_preview", preview)
	monkeypatch.setattr(images, "_ensure_chat_preview", preview)
	return types.SimpleNamespace(
		f=f, store=store, media=media, images=images, preview=preview,
		ensure_preview=ensure_preview, save_file=file_manager.save_file,
	)


def duplicate_attachments(env, crm_first=True):
	env.store.add_document()
	files = [
		{"name": "crmfile001", "attached_to_doctype": "CRM Product", "readable": False},
		{"name": "itemfile01"},
	]
	for values in files if crm_first else reversed(files):
		env.store.add_file(**values)


@pytest.mark.parametrize("crm_first", [True, False])
@pytest.mark.parametrize("reference", [IMAGE_URL, "itemfile01", None])
def test_document_image_selects_its_own_file_regardless_of_duplicate_insertion_order(env, crm_first, reference):
	duplicate_attachments(env, crm_first)
	result = env.images.show_image(file=reference, doctype="Item", name="ITEM-001", field="image")
	assert result["ok"] is True
	assert result["file_id"] == "itemfile01"
	assert result["url"] == "/private/files/chat-preview-itemfile01.jpg"
	assert result["file_name"] == "shared-product.png"
	assert result["markdown"] == f"![{result['alt']}]({result['url']})"
	assert env.store.docs[("Item", "ITEM-001")].permission_checks == ["read"]
	assert env.store.docs[("File", "itemfile01")].permission_checks
	assert not env.store.docs[("File", "crmfile001")].permission_checks


@pytest.mark.parametrize("reference", [IMAGE_URL, "shared-product.png"])
def test_ambiguous_url_or_filename_requires_explicit_attachment_identity(env, reference):
	duplicate_attachments(env)
	with pytest.raises(ValidationError) as error:
		env.images.show_image(file=reference)
	assert str(error.value).strip()
	env.preview.assert_not_called()


def test_exact_denied_file_does_not_fall_back_to_an_allowed_alias(env):
	duplicate_attachments(env)
	with pytest.raises(PermissionError) as error:
		env.images.show_image(file="crmfile001")
	assert str(error.value).strip()
	assert not env.store.docs[("File", "itemfile01")].permission_checks
	env.preview.assert_not_called()


def test_parent_permission_is_required_even_when_file_is_readable(env):
	parent = env.store.add_document(readable=False)
	env.store.add_file()
	with pytest.raises(PermissionError):
		env.images.show_image(file="itemfile01", doctype="Item", name="ITEM-001")
	assert parent.permission_checks == ["read"]
	env.preview.assert_not_called()


@pytest.mark.parametrize("context", [{"doctype": "Item"}, {"name": "ITEM-001"}, {"field": "image"}])
def test_partial_document_context_cannot_be_bypassed_with_a_file(env, context):
	env.store.add_file()
	with pytest.raises(ValidationError):
		env.images.show_image(file="itemfile01", **context)
	env.preview.assert_not_called()


@pytest.mark.parametrize("mismatch", ["doctype", "name", "field", "url"])
def test_explicit_file_must_match_the_requested_document_field(env, mismatch):
	env.store.add_document()
	env.store.add_file()
	values = {"name": "wrongfile1"}
	values.update({
		"doctype": {"attached_to_doctype": "CRM Product"},
		"name": {"attached_to_name": "ITEM-002"},
		"field": {"attached_to_field": "website_image"},
		"url": {"file_url": "/private/files/different.png"},
	}[mismatch])
	env.store.add_file(**values)
	with pytest.raises(ValidationError):
		env.images.show_image(file="wrongfile1", doctype="Item", name="ITEM-001", field="image")
	env.preview.assert_not_called()


@pytest.mark.parametrize("missing", ["field", "image", "attachment"])
def test_missing_requested_image_never_substitutes_an_unrelated_attachment(env, missing):
	env.store.add_document(image="" if missing == "image" else IMAGE_URL)
	env.store.add_file(
		name="otherfile1", file_url="/private/files/unrelated.png", attached_to_field="other_image",
	)
	with pytest.raises(ValidationError):
		env.images.show_image(
			doctype="Item", name="ITEM-001", field="missing_field" if missing == "field" else "image",
		)
	env.preview.assert_not_called()


def test_duplicate_files_within_same_document_field_are_still_ambiguous(env):
	env.store.add_document()
	env.store.add_file()
	env.store.add_file(name="itemfile02")
	with pytest.raises(ValidationError):
		env.images.show_image(doctype="Item", name="ITEM-001", field="image")
	env.preview.assert_not_called()


@pytest.mark.parametrize("reference", [IMAGE_URL, "shared-product.png", "itemfile01"])
def test_unique_legacy_file_references_still_resolve(env, reference):
	env.store.add_file()
	result = env.images.show_image(file=reference, alt="Product photo")
	assert result["file_id"] == "itemfile01"
	assert result["alt"] == "Product photo"
	assert result["url"] == "/private/files/chat-preview-itemfile01.jpg"


def test_document_reference_defaults_to_its_image_field(env):
	env.store.add_document(website_image="/private/files/website.png")
	env.store.add_file()
	env.store.add_file(
		name="website001", file_url="/private/files/website.png", attached_to_field="website_image",
	)
	assert env.images.show_image(doctype="Item", name="ITEM-001")["file_id"] == "itemfile01"


def test_non_image_attachment_is_rejected_before_preview_creation(env):
	env.store.add_file(file_name="invoice.pdf", file_url="/private/files/invoice.pdf")
	with pytest.raises(ValidationError):
		env.images.show_image(file="itemfile01")
	env.preview.assert_not_called()


def add_preview(env, **values):
	url_prefix = "/private/files" if values.get("is_private", 1) else "/files"
	return env.store.add_file(
		name="preview001", file_name="chat-preview-origfile01.jpg",
		file_url=f"{url_prefix}/chat-preview-origfile01.jpg", **values,
	)


def test_chat_preview_opens_its_exact_original_when_original_url_has_other_files(env):
	env.store.add_file(name="stalecrm01", attached_to_doctype="CRM Product", readable=False)
	original = env.store.add_file(name="origfile01")
	preview = add_preview(env)
	result = env.media.get_chat_original(file=preview.file_url)
	assert result == {"url": IMAGE_URL + "?fid=origfile01", "file_name": "shared-product.png"}
	assert preview.permission_checks == ["read"]
	assert original.permission_checks == ["read"]


@pytest.mark.parametrize("denied", ["preview", "original"])
def test_preview_and_original_both_require_their_own_file_read_permission(env, denied):
	env.store.add_file(name="origfile01", readable=denied != "original")
	preview = add_preview(env, readable=denied != "preview")
	with pytest.raises(PermissionError):
		env.media.get_chat_original(file=preview.file_url)


@pytest.mark.parametrize("mismatch", ["doctype", "name", "privacy"])
def test_preview_cannot_resolve_an_original_from_another_attachment_scope(env, mismatch):
	env.store.add_file(name="origfile01")
	values = {
		"doctype": {"attached_to_doctype": "CRM Product"},
		"name": {"attached_to_name": "ITEM-002"},
		"privacy": {"is_private": 0},
	}[mismatch]
	preview = add_preview(env, **values)
	with pytest.raises(ValidationError):
		env.media.get_chat_original(file=preview.file_url)


def test_preview_cache_reuses_a_readable_preview_with_same_parent_and_privacy(env):
	original = env.store.add_file(name="origfile01")
	preview = add_preview(env)
	assert env.ensure_preview(original) == preview.file_url + "?fid=preview001"
	assert preview.permission_checks == ["read"]
	env.save_file.assert_not_called()
	assert not env.store.inserted


def test_preview_cache_checks_cached_file_read_permission(env):
	original = env.store.add_file(name="origfile01")
	preview = add_preview(env, readable=False)
	with pytest.raises(PermissionError) as error:
		env.ensure_preview(original)
	assert str(error.value).strip()
	assert preview.permission_checks == ["read"]
	env.save_file.assert_not_called()
	assert not env.store.inserted


@pytest.mark.parametrize("mismatch", ["doctype", "name", "privacy"])
def test_preview_cache_never_reuses_a_foreign_attachment(env, mismatch):
	original = env.store.add_file(name="origfile01")
	values = {
		"doctype": {"attached_to_doctype": "CRM Product"},
		"name": {"attached_to_name": "ITEM-002"},
		"privacy": {"is_private": 0},
	}[mismatch]
	foreign = add_preview(env, **values)
	foreign.file_url = "/private/files/foreign-preview.jpg" if foreign.is_private else "/files/foreign-preview.jpg"
	url = env.ensure_preview(original)
	assert urlsplit(url).path != foreign.file_url
	assert not foreign.permission_checks
	assert len(env.store.inserted) == 1
	assert parse_qs(urlsplit(url).query) == {"fid": [env.store.inserted[0].name]}
	env.save_file.assert_not_called()


@pytest.mark.parametrize("same_parent", [True, False])
@pytest.mark.parametrize("is_private", [0, 1])
def test_native_preview_insert_preserves_identity_when_two_previews_share_bytes_and_url(env, same_parent, is_private):
	url_prefix = "/private/files" if is_private else "/files"
	originals = [
		env.store.add_file(name="origfile01", file_url=f"{url_prefix}/original-one.png", is_private=is_private),
		env.store.add_file(
			name="origfile02", file_url=f"{url_prefix}/original-two.png", is_private=is_private,
			attached_to_name="ITEM-001" if same_parent else "ITEM-002",
		),
	]
	urls = [env.ensure_preview(original) for original in originals]
	assert len(env.store.inserted) == 2
	first, second = env.store.inserted
	assert first.file_url == second.file_url
	assert first.name != second.name
	assert first.file_name == "chat-preview-origfile01.jpg"
	assert second.file_name == "chat-preview-origfile02.jpg"
	assert first.content_hash == second.content_hash
	for original, preview, url in zip(originals, env.store.inserted, urls, strict=True):
		assert urlsplit(url).path == preview.file_url
		assert parse_qs(urlsplit(url).query) == {"fid": [preview.name]}
		assert preview.attached_to_name == original.attached_to_name
		assert preview.is_private == original.is_private
		assert env.ensure_preview(original) == url
		assert env.media.get_chat_original(file=url) == {
			"url": original.file_url + f"?fid={original.name}", "file_name": original.file_name,
		}
	assert len(env.store.inserted) == 2
	env.save_file.assert_not_called()


@pytest.mark.parametrize("reference", [
	IMAGE_URL + "?fid=missing001",
	IMAGE_URL + "?fid=",
	IMAGE_URL + "?fid=itemfile01&fid=itemfile01",
	"/private/files/other.png?fid=itemfile01",
	"https://foreign.example.test" + IMAGE_URL + "?fid=itemfile01",
])
def test_invalid_fid_url_never_falls_back_to_a_different_attachment(env, reference):
	env.store.add_file()
	env.store.add_file(name="otherfile1", file_url="/private/files/other.png")
	with pytest.raises(ValidationError) as error:
		env.media.get_chat_original(file=reference)
	assert str(error.value).strip()


def test_shared_url_with_fid_checks_the_identified_private_file_only(env):
	duplicate_attachments(env)
	with pytest.raises(PermissionError) as error:
		env.media.get_chat_original(file=IMAGE_URL + "?fid=crmfile001")
	assert str(error.value).strip()
	assert not env.store.docs[("File", "itemfile01")].permission_checks
	assert env.media.get_chat_original(file=IMAGE_URL + "?fid=itemfile01") == {
		"url": IMAGE_URL + "?fid=itemfile01", "file_name": "shared-product.png",
	}


def test_fid_cannot_override_the_requested_document_scope(env):
	env.store.add_document()
	env.store.add_file()
	env.store.add_file(name="otherfile1", attached_to_name="ITEM-002")
	with pytest.raises(ValidationError):
		env.images.show_image(
			file=IMAGE_URL + "?fid=otherfile1", doctype="Item", name="ITEM-001", field="image",
		)
	env.preview.assert_not_called()


def test_shared_preview_url_without_fid_requires_explicit_identity(env):
	original = env.store.add_file(name="origfile01")
	other = env.store.add_file(name="origfile02", attached_to_name="ITEM-002")
	urls = [env.ensure_preview(doc) for doc in [original, other]]
	assert urlsplit(urls[0]).path == urlsplit(urls[1]).path
	with pytest.raises(ValidationError) as error:
		env.media.get_chat_original(file=urlsplit(urls[0]).path)
	assert str(error.value).strip()
