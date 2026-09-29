"""Sticker image transaction checks with an isolated database and real image decoding."""

import copy
import importlib.util
import sys
import types
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image


class Row(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__


class Doc:
	def __init__(self, harness, values):
		object.__setattr__(self, "h", harness)
		object.__setattr__(self, "values", copy.deepcopy(values))
		for key in ("attributes", "item_attribute_values"):
			if key in self.values:
				self.values[key] = [Row(row) for row in self.values[key]]

	def __getattr__(self, key):
		return self.values.get(key)

	def __setattr__(self, key, value):
		self.values[key] = value

	def check_permission(self, perm):
		self.h.permission(self.doctype, perm, doc=self, throw=True)

	def append(self, key, value):
		self.values[key].append(Row(value))

	def insert(self):
		self.check_permission("create")
		if self.doctype == "File":
			self.h.get_doc(self.attached_to_doctype, self.attached_to_name).check_permission("write")
			self.name = f"attachment-{len(self.h.data['File']) + 1}"
		else:
			self.name = self.item_code
		self._persist()
		if self.h.fail_after == self.doctype:
			raise RuntimeError("injected native insert failure")
		return self

	def save(self):
		self.check_permission("write")
		self._persist()
		if self.h.fail_after == "Item save" and self.doctype == "Item":
			raise RuntimeError("injected native save failure")
		return self

	def _persist(self):
		self.h.data[self.doctype][self.name] = copy.deepcopy(self.values)
		self.h.writes += 1

	def get_content(self, encodings=None):
		self.h.read_images.append(self.name)
		return self.h.blobs[self.file_url]

	def create_attachment_copy(self, doctype, name, field):
		values = {**self.values, "name": None, "attached_to_doctype": doctype,
			"attached_to_name": name, "attached_to_field": field}
		if self.h.corrupt_attachment:
			values["is_private"] = 0
		return Doc(self.h, values).insert()


class Harness:
	def __init__(self, monkeypatch):
		self.data = {"Customer": {"CUST-1": {"doctype": "Customer", "name": "CUST-1", "customer_name": "Jesse Johnson", "disabled": 0}},
			"Item": {"巧克粉贴纸": {"doctype": "Item", "name": "巧克粉贴纸", "item_code": "巧克粉贴纸", "item_name": "巧克粉贴纸", "has_variants": 1,
				"disabled": 0, "variant_based_on": "Item Attribute", "attributes": [{"attribute": key} for key in ("客户", "贴纸型号", "贴纸版本")]}},
			"Item Attribute": {key: {"doctype": "Item Attribute", "name": key, "item_attribute_values": [], "disabled": 0, "numeric_values": 0} for key in ("客户", "贴纸型号", "贴纸版本")},
			"File": {}}
		self.data["Item Attribute"]["贴纸型号"]["item_attribute_values"] = [
			{"attribute_value": model, "abbr": model}
			for model in ("山东中性方形", "广东油性方形", "圆形")
		]
		self.denied = set()
		self.saved = {}
		self.blobs = {}
		self.read_images = []
		self.writes = 0
		self.fail_after = None
		self.corrupt_attachment = False
		self.add_image("upload-1", "/private/files/sticker.png")
		frappe = types.ModuleType("frappe")
		frappe.PermissionError = PermissionError
		frappe.ValidationError = ValueError
		frappe.flags = Row()
		frappe.get_doc = self.get_doc
		frappe.get_all = self.get_all
		frappe.has_permission = self.permission
		frappe.throw = lambda message: (_ for _ in ()).throw(ValueError(message))
		frappe.clear_document_cache = lambda *args: None
		frappe.db = types.SimpleNamespace(sql=self.sql, exists=lambda dt, name: name in self.data[dt],
			get_value=self.get_value, count=lambda dt, filters: len(self.get_all(dt, filters=filters)),
			savepoint=self.savepoint, rollback=self.rollback)
		monkeypatch.setitem(sys.modules, "frappe", frappe)
		variant_module = types.ModuleType("erpnext.controllers.item_variant")
		variant_module.create_variant = self.create_variant
		monkeypatch.setitem(sys.modules, variant_module.__name__, variant_module)
		spec = importlib.util.spec_from_file_location("sticker_under_test", Path(__file__).resolve().parents[2] / "flow/integrations/erpnext/sticker_variant.py")
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)

	def add_image(self, name, url, attached_to_doctype="Flow Session", attached_to_name="session-1"):
		self.data["File"][name] = {"doctype": "File", "name": name, "file_url": url, "file_name": url.rsplit("/", 1)[-1],
			"is_private": int(url.startswith("/private/")), "owner": "agent@example.com", "attached_to_doctype": attached_to_doctype,
			"attached_to_name": attached_to_name, "is_folder": 0}
		image = BytesIO()
		Image.new("RGB", (4, 4), "white").save(image, format="PNG")
		self.blobs[url] = image.getvalue()

	def get_doc(self, doctype, name=None, **kwargs):
		return Doc(self, self.data[doctype][name])

	def get_all(self, doctype, filters=None, pluck=None, limit=None, **kwargs):
		values = [Row(copy.deepcopy(row)) for row in self.data[doctype].values() if all(row.get(key) == value for key, value in (filters or {}).items())]
		values = values[:limit] if limit else values
		return [value[pluck] for value in values] if pluck else values

	def get_value(self, doctype, filters, field):
		if isinstance(filters, str):
			return self.data[doctype].get(filters, {}).get(field)
		rows = self.get_all(doctype, filters=filters)
		return rows[0].get(field) if rows else None

	def permission(self, doctype, perm, doc=None, throw=False):
		allowed = (doctype, perm) not in self.denied and (doctype, getattr(doc, "name", None), perm) not in self.denied
		if throw and not allowed:
			raise PermissionError("denied")
		return allowed

	def savepoint(self, name):
		self.saved[name] = copy.deepcopy(self.data)

	def rollback(self, save_point):
		self.data = copy.deepcopy(self.saved[save_point])

	def sql(self, query, args, as_dict=False):
		assert "tabItem Variant Attribute" in query
		return [Row(parent=name, **row) for name, item in self.data["Item"].items() if item.get("variant_of") == args[0] for row in item["attributes"]]

	def create_variant(self, name, target):
		attrs = self.data["Item"][name]["attributes"]
		abbrs = {key: next(row["abbr"] for row in self.data["Item Attribute"][key]["item_attribute_values"] if row["attribute_value"] == value) for key, value in target.items()}
		code = name + "-" + "-".join(abbrs[row["attribute"]] for row in attrs)
		return Doc(self, {"doctype": "Item", "item_code": code, "item_name": code, "variant_of": name, "disabled": 0, "has_variants": 0,
			"image": "/files/template.png", "attributes": [{"attribute": key, "attribute_value": value} for key, value in target.items()]})

	def call(self, **kwargs):
		return self.module.create_customer_sticker_variant(**{"customer_name": "Jesse Johnson", "sticker_model": "山东中性方形模版", "sticker_version": "V1", "image_file": "upload-1", **kwargs})


@pytest.fixture
def harness(monkeypatch):
	return Harness(monkeypatch)


def test_creation_saves_private_image_without_moving_chat_attachment(harness):
	original = copy.deepcopy(harness.data["File"]["upload-1"])
	result = harness.call()
	assert result["status"] == "created", result
	assert result["verified"] and result["image_verified"]
	assert harness.data["Item"][result["item_code"]]["image"] == original["file_url"]
	assert harness.data["File"]["upload-1"] == original
	assert result["image_file"] != "upload-1"
	assert harness.data["File"][result["image_file"]]["is_private"] == 1
	assert all(row["action"] and row["reason"] and row["status"] not in {"running", "not_executed"} for row in result["steps"])
	assert result["attributes"]["贴纸型号"] == "山东中性方形"
	assert all(row["attribute"] != "贴纸型号" for row in result["registered_attribute_values"])


@pytest.mark.parametrize("model", ["", "方形", "三角形", "山东中性方形和广东油性方形"])
def test_ambiguous_or_unknown_model_cannot_add_values_or_create_item(harness, model):
	before = copy.deepcopy(harness.data)
	result = harness.call(sticker_model=model)
	assert result["status"] == "needs_input" and result["missing"] == ["sticker_model"], result
	assert result["allowed_models"] == ["山东中性方形", "广东油性方形", "圆形"]
	assert all(value in result["message"] for value in result["allowed_models"])
	assert harness.data == before and harness.writes == 0


def test_missing_standard_model_is_not_silently_registered(harness):
	harness.data["Item Attribute"]["贴纸型号"]["item_attribute_values"] = []
	before = copy.deepcopy(harness.data)
	result = harness.call()
	assert result["status"] == "error" and "客服工具不会新增型号" in result["message"], result
	assert harness.data == before and harness.writes == 0


@pytest.mark.parametrize("model", ["山东中性方形", "广东油性方形", "圆形"])
def test_all_standard_models_can_create_complete_stickers(harness, model):
	result = harness.call(sticker_model=model)
	assert result["status"] == "created" and result["attributes"]["贴纸型号"] == model, result
	assert result["image_verified"]


def test_missing_image_does_not_register_attributes_or_create_item(harness):
	before = copy.deepcopy(harness.data)
	result = harness.call(image_file="")
	assert result["status"] == "needs_input" and result["missing"] == ["image_file"], result
	assert harness.data == before and harness.writes == 0


def test_missing_version_only_asks_for_version(harness):
	result = harness.call(sticker_version="")
	assert result["missing"] == ["sticker_version"]
	assert result["message"] == "请补充：贴纸版本。"
	assert harness.writes == 0


@pytest.mark.parametrize("permission", [("File", "upload-1", "read"), ("File", "create"), ("Item", "create"), ("Item Attribute", "write")])
def test_permissions_are_enforced_without_partial_records(harness, permission):
	harness.denied.add(permission)
	before = copy.deepcopy(harness.data)
	result = harness.call()
	assert result["status"] == "error" and result["error_type"] == "PermissionError", result
	assert harness.data == before
	if permission == ("File", "upload-1", "read"):
		assert harness.read_images == [] and "image_url" not in result


def test_url_uses_accessible_file_without_leaking_an_unreadable_copy(harness):
	harness.denied.add(("File", "upload-1", "read"))
	harness.add_image("upload-2", "/private/files/sticker.png", "Item", "other-item")
	result = harness.call(image_file="/private/files/sticker.png")
	assert result["status"] == "created", result
	assert harness.read_images == ["upload-2"]
	assert harness.data["File"]["upload-2"]["attached_to_name"] == "other-item"


def test_retry_is_idempotent_and_existing_image_can_be_reused(harness):
	first = harness.call()
	before = copy.deepcopy(harness.data)
	for kwargs in ({}, {"image_file": ""}):
		result = harness.call(**kwargs)
		assert result["status"] == "existing" and result["image_file"] == first["image_file"], result
		assert harness.data == before


def test_existing_image_requires_explicit_replacement(harness):
	first = harness.call()
	harness.add_image("new-upload", "/private/files/new.png")
	before = copy.deepcopy(harness.data)
	blocked = harness.call(image_file="new-upload")
	assert blocked["status"] == "needs_input" and blocked["missing"] == ["replace_image"], blocked
	assert harness.data == before
	result = harness.call(image_file="new-upload", replace_image=True)
	assert result["status"] == "existing" and result["image_url"] == "/private/files/new.png", result
	assert first["image_file"] in harness.data["File"]
	assert len(harness.data["Item"]) == 2


def test_existing_read_only_item_can_reuse_complete_image(harness):
	first = harness.call()
	harness.denied.update({("Item", "write"), ("File", "create")})
	result = harness.call(image_file="")
	assert result["status"] == "existing" and result["image_file"] == first["image_file"], result


@pytest.mark.parametrize("failure", ["Item", "File"])
def test_native_insert_failure_rolls_back_attributes_item_and_attachment(harness, failure):
	before = copy.deepcopy(harness.data)
	harness.fail_after = failure
	result = harness.call()
	assert result["status"] == "error" and result["rolled_back"], result
	assert harness.data == before
	assert any(row["status"] == "rolled_back" for row in result["steps"])
	assert not any(row["status"] == "saved" for row in result["steps"])


def test_image_save_failure_restores_existing_item_and_attachment(harness):
	harness.call()
	harness.add_image("new-upload", "/private/files/new.png")
	before = copy.deepcopy(harness.data)
	harness.fail_after = "Item save"
	result = harness.call(image_file="new-upload", replace_image=True)
	assert result["status"] == "error" and result["failed_step"] == "save_image", result
	assert harness.data == before


def test_verification_mismatch_rolls_back_everything(harness):
	before = copy.deepcopy(harness.data)
	harness.corrupt_attachment = True
	result = harness.call()
	assert result["status"] == "error" and result["failed_step"] == "verify", result
	assert harness.data == before


@pytest.mark.parametrize("url", ["https://example.com/x.png", "/private/files/../secret.png", "/private/files/%2e%2e/secret.png", "javascript:alert(1)"])
def test_external_and_unsafe_paths_are_rejected_before_writes(harness, url):
	result = harness.call(image_file=url)
	assert result["status"] == "error", result
	assert harness.writes == 0 and harness.read_images == []


def test_fake_image_is_not_accepted_by_extension_alone(harness):
	harness.blobs["/private/files/sticker.png"] = b"<html>not an image</html>"
	result = harness.call()
	assert result["status"] == "needs_input" and result["missing"] == ["image_file"], result
	assert harness.writes == 0


def test_preview_does_not_mutate_and_disabled_existing_is_not_enabled(harness):
	before = copy.deepcopy(harness.data)
	preview = harness.call(dry_run=True)
	assert preview["status"] == "preview" and harness.data == before
	created = harness.call()
	harness.data["Item"][created["item_code"]]["disabled"] = 1
	before = copy.deepcopy(harness.data)
	result = harness.call(image_file="")
	assert result["status"] == "existing_disabled" and harness.data == before
	assert "image_url" not in result


def test_new_sticker_explicitly_enables_stock_and_does_not_receive_it(harness):
	result = harness.call()
	assert result['is_stock_item'] and result['stock_receipt_created'] is False
	assert harness.data['Item'][result['item_code']]['is_stock_item'] == 1


def test_existing_non_stock_sticker_is_not_silently_reused_or_reclassified(harness):
	created = harness.call()
	harness.data['Item'][created['item_code']]['is_stock_item'] = 0
	before = copy.deepcopy(harness.data)
	result = harness.call()
	assert result['status'] == 'error' and '维护库存' in result['message']
	assert harness.data == before
