"""Permission-aware, atomic customer sticker variants for the imported Flow tool."""

from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit
from uuid import uuid4
import warnings

import frappe
from erpnext.controllers.item_variant import create_variant
from PIL import Image

from erpnext.stock.doctype.item.sticker_models import ALLOWED_MODELS, normalize_model


TEMPLATE = "巧克粉贴纸"
ATTRIBUTES = ("客户", "贴纸型号", "贴纸版本")
FIELD_LABELS = {"customer_name": "客户", "sticker_model": "贴纸型号", "sticker_version": "贴纸版本", "image_file": "贴纸图片", "replace_image": "确认替换现有图片"}
STEP_LABELS = {
	"check_inputs": "检查必填资料", "match_variant": "核对客户、模板和已有贴纸",
	"check_image": "检查图片及访问权限", "save_attributes": "登记贴纸属性",
	"save_item": "保存贴纸物料", "save_image": "关联贴纸图片", "verify": "回读核验贴纸和图片",
}


class NeedsInput(Exception):
	def __init__(self, message, **details):
		super().__init__(message)
		self.details = details


class StickerSteps:
	def __init__(self):
		self.rows = []
		self.current = None

	def begin(self, key, action):
		self.current = {"step": key, "label": STEP_LABELS[key], "status": "running", "action": action, "reason": "正在执行。"}
		self.rows.append(self.current)

	def done(self, reason, status="passed"):
		self.current.update(status=status, reason=reason)

	def fail(self, reason, rolled_back=False):
		self.current.update(status="failed", reason=reason)
		if rolled_back:
			self.current["rolled_back"] = True
			for row in self.rows:
				if row["status"] == "saved":
					row.update(status="rolled_back", reason=row["reason"] + " 后续步骤未通过，本次修改已回滚。")
		return self.current["step"]

	def result(self, result):
		seen = {row["step"] for row in self.rows}
		if result["status"] == "preview":
			reason = "本次仅预检，尚未写入或回读保存结果。"
		elif result["status"] == "existing_disabled":
			reason = "已有贴纸已停用，未修改其物料、属性或图片。"
		else:
			reason = "前序步骤未通过，因此未执行。"
		return {**result, "steps": self.rows + [
			{"step": key, "label": label, "status": "not_executed", "action": label, "reason": reason}
			for key, label in STEP_LABELS.items() if key not in seen
		]}


def _missing(fields):
	labels = [FIELD_LABELS[field] for field in fields]
	return {"status": "needs_input", "missing": fields, "missing_labels": labels, "message": "请补充：" + "、".join(labels) + "。"}


def _key(value):
	return (value or "").strip().casefold()


def _text(value, label):
	if not isinstance(value, str):
		frappe.throw(f"{label}必须是文本。")
	value = value.strip()
	if len(value) > 140 or any(ord(c) < 32 or c in "<>" for c in value):
		frappe.throw(f"{label}过长或包含不支持的字符。")
	return value


def _customer(value):
	name = frappe.db.get_value("Customer", value, "name")
	if not name:
		matches = frappe.get_all("Customer", filters={"customer_name": value}, pluck="name", limit=2)
		if len(matches) != 1:
			raise NeedsInput("客户名称不存在或不唯一，请提供明确的客户编号。", missing=["customer_name"], missing_labels=["客户"])
		name = matches[0]
	doc = frappe.get_doc("Customer", name)
	doc.check_permission("read")
	if doc.disabled:
		frappe.throw("该客户已停用，不能创建贴纸变体。")
	# The existing template identifies customers by full name, not document ID.
	# Do not silently reuse another customer's item when display names coincide.
	if frappe.db.count("Customer", {"customer_name": doc.customer_name}) != 1:
		frappe.throw("多个客户使用相同完整名称；现有客户属性无法区分，请先整理客户名称。")
	return doc


def _template(lock):
	doc = frappe.get_doc("Item", TEMPLATE, for_update=lock)
	doc.check_permission("read")
	configured = [row.attribute for row in doc.attributes]
	if (doc.disabled or not doc.has_variants or doc.variant_based_on != "Item Attribute"
		or len(configured) != 3 or set(configured) != set(ATTRIBUTES)):
		frappe.throw("巧克粉贴纸模板必须启用，并且恰好配置客户、贴纸型号、贴纸版本三个文本属性。")
	return doc


def _existing(target, lock=False):
	# Global duplicate detection must not depend on list visibility or a page limit.
	rows = frappe.db.sql("""
		select a.parent, a.attribute, a.attribute_value
		from `tabItem Variant Attribute` a
		inner join `tabItem` i on i.name = a.parent
		where i.variant_of = %s and a.parenttype = 'Item'
			and a.parentfield = 'attributes'
	""" + (" for update" if lock else ""), (TEMPLATE,), as_dict=True)
	combinations = {}
	for row in rows:
		combinations.setdefault(row.parent, []).append((row.attribute, _key(row.attribute_value)))
	expected = sorted((attribute, _key(value)) for attribute, value in target.items())
	matches = [name for name, values in combinations.items() if sorted(values) == expected]
	if len(matches) > 1:
		frappe.throw("这个属性组合已存在多个物料，请先人工核对重复物料。")
	if not matches:
		return None
	doc = frappe.get_doc("Item", matches[0])
	doc.check_permission("read")
	return doc


def _result(doc, status, registered=None, image=None):
	result = {
		"status": status,
		"item_code": doc.name,
		"item_name": doc.item_name,
		"variant_of": doc.variant_of,
		"disabled": bool(doc.disabled),
		"is_stock_item": bool(doc.is_stock_item),
		"stock_receipt_created": False,
		"attributes": {row.attribute: row.attribute_value for row in doc.attributes},
		"registered_attribute_values": registered or [],
		"verified": True,
		"message": "已有物料已停用，未新建或自动启用。" if doc.disabled else (
			"贴纸及图片已保存并核验，可直接展示图片供检查。" if status == "created" else "已复用现有贴纸并核验图片，可直接展示图片供检查。"
		),
	}
	if image:
		result.update(image_file=image.name, image_url=image.file_url, image_file_name=image.file_name,
			image_is_private=bool(image.is_private), image_verified=True)
	return result


def _local_image_url(value):
	parsed = urlsplit(value)
	path = unquote(parsed.path)
	if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
		or not path.startswith(("/files/", "/private/files/"))
		or any(part in {".", ".."} for part in path.split("/"))
		or any(ord(char) < 32 or char in "\\<>" for char in path)):
		frappe.throw("贴纸图片必须是本站已上传的图片附件，不能使用外部地址或无效路径。")
	return path


def _image_file(value):
	"""Resolve only readable local File documents, then validate actual image data."""
	if frappe.db.exists("File", value):
		doc = frappe.get_doc("File", value)
		doc.check_permission("read")
	else:
		if not value.startswith("/") and not urlsplit(value).scheme:
			raise NeedsInput("没有找到可用的贴纸图片，请上传图片后继续。", missing=["image_file"], missing_labels=["贴纸图片"])
		path = _local_image_url(value)
		names = frappe.get_all("File", filters={"file_url": path}, pluck="name", order_by="creation asc")
		doc = None
		for name in names:
			candidate = frappe.get_doc("File", name)
			try:
				candidate.check_permission("read")
			except frappe.PermissionError:
				continue
			doc = candidate
			break
		if not doc:
			if names:
				raise frappe.PermissionError("当前用户无权读取该图片附件。")
			raise NeedsInput("没有找到可用的贴纸图片，请上传图片后继续。", missing=["image_file"], missing_labels=["贴纸图片"])
	path = _local_image_url(doc.file_url or "")
	if doc.is_folder or PurePosixPath(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}:
		raise NeedsInput("附件不是支持的贴纸图片，请上传 PNG、JPG 或其他常见图片文件。", missing=["image_file"], missing_labels=["贴纸图片"])
	if bool(doc.is_private) != path.startswith("/private/files/"):
		frappe.throw("图片附件的私有状态与文件路径不一致，请先修复附件记录。")
	try:
		with warnings.catch_warnings():
			warnings.simplefilter("error", Image.DecompressionBombWarning)
			with Image.open(BytesIO(doc.get_content(encodings=[]))) as picture:
				picture.verify()
	except (OSError, ValueError, TypeError, Image.DecompressionBombError, Image.DecompressionBombWarning):
		raise NeedsInput("图片文件无法完整读取或格式无效，请重新上传清晰、完整的图片。", missing=["image_file"], missing_labels=["贴纸图片"])
	return doc


def _attachment_for_item(source, item):
	for name in frappe.get_all("File", filters={"file_url": source.file_url,
		"attached_to_doctype": "Item", "attached_to_name": item.name}, pluck="name", order_by="creation asc"):
		doc = frappe.get_doc("File", name)
		doc.check_permission("read")
		if bool(doc.is_private) == bool(source.is_private):
			return doc
	return None


def _save_image(source, item):
	attachment = _attachment_for_item(source, item)
	if not attachment:
		# Native copy retains the original attachment and blob, including privacy.
		# It executes File create and target-document permission checks normally.
		item.check_permission("write")
		attachment = source.create_attachment_copy("Item", item.name, "image")
	if item.image != source.file_url:
		item.check_permission("write")
		item.image = source.file_url
		item.save()
	return attachment


def _verify_image(item, attachment, source):
	verified = frappe.get_doc("Item", item.name)
	verified.check_permission("read")
	if not verified.is_stock_item:
		frappe.throw("客户贴纸必须启用‘维护库存’，物料配置核验未通过。")
	image = frappe.get_doc("File", attachment.name)
	image.check_permission("read")
	if (verified.image != source.file_url or image.file_url != source.file_url
		or bool(image.is_private) != bool(source.is_private)
		or image.attached_to_doctype != "Item" or image.attached_to_name != verified.name):
		frappe.throw("贴纸图片关联回读核验失败，本次修改已回滚。")
	return verified, image


def create_customer_sticker_variant(
	customer_name: str, sticker_model: str, sticker_version: str, dry_run: bool = False,
	image_file: str = "", replace_image: bool = False,
) -> dict:
	"""Create/reuse a complete sticker with its image in one approved operation.

	The outer Flow request owns commit. A savepoint isolates all writes so even
	callers that commit a returned error cannot retain a partial attribute/item/file.
	image_file accepts an uploaded File name or local URL. A usable existing item
	image may be reused; replacing another image requires explicit replace_image.
	"""
	steps = StickerSteps()
	steps.begin("check_inputs", "核对客户、贴纸型号、版本及图片参数，只补问实际缺少的资料。")
	missing = [key for key, value in {
		"customer_name": customer_name, "sticker_model": sticker_model,
		"sticker_version": sticker_version,
	}.items() if value is None or (isinstance(value, str) and not value.strip())]
	if missing:
		result = _missing(missing)
		if "sticker_model" in missing:
			result.update(allowed_models=list(ALLOWED_MODELS))
			result["message"] += f"可选贴纸型号：{'、'.join(ALLOWED_MODELS)}。"
		result["failed_step"] = steps.fail(result["message"])
		return steps.result(result)
	if type(dry_run) is not bool or type(replace_image) is not bool or not isinstance(image_file, str):
		message = "dry_run、replace_image 必须为 true 或 false，image_file 必须为图片附件编号或路径文本。"
		return steps.result({"status": "needs_input", "message": message, "failed_step": steps.fail(message)})
	try:
		model = normalize_model(sticker_model)
	except ValueError as exc:
		message = f"{exc} 可选贴纸型号：{'、'.join(ALLOWED_MODELS)}。"
		return steps.result({"status": "needs_input", "message": message,
			"missing": ["sticker_model"], "missing_labels": ["贴纸型号"],
			"allowed_models": list(ALLOWED_MODELS), "failed_step": steps.fail(message)})
	steps.done("客户、型号和版本已提供，继续匹配已有贴纸及图片。")

	savepoint = "sticker_" + uuid4().hex
	frappe.db.savepoint(savepoint)
	touched = []
	try:
		steps.begin("match_variant", "读取当前用户可访问的客户和模板，按客户、型号、版本查重。")
		customer_name = _text(customer_name, "客户")
		version = _text(sticker_version, "贴纸版本")
		customer = _customer(customer_name)
		template = _template(lock=not dry_run)
		target = {"客户": _text(customer.customer_name, "客户完整名称"), "贴纸型号": model, "贴纸版本": version}
		existing = _existing(target, lock=not dry_run)
		if existing and not existing.is_stock_item:
			frappe.throw("已有客户贴纸未启用‘维护库存’，请管理员通过原生物料设置修复后再使用，不能直接作为非库存物料出库。")
		if existing and existing.disabled:
			steps.done("已找到同属性贴纸，但物料已停用，不能作为可用贴纸返回。", "not_required")
			return steps.result(_result(existing, "existing_disabled"))
		steps.done("已有相同属性的贴纸，将复用并核对图片。" if existing else "客户和模板可用，未发现重复贴纸。")

		steps.begin("check_image", "核验本次上传图片或已有贴纸图片，并检查是否会覆盖原图。")
		image_file = image_file.strip()
		if not image_file and not (existing and existing.image):
			raise NeedsInput("请补充：贴纸图片。图片齐全后再一次性创建并登记。", missing=["image_file"], missing_labels=["贴纸图片"])
		source = _image_file(image_file or existing.image)
		if existing and existing.image and unquote(existing.image) != source.file_url and not replace_image:
			raise NeedsInput("该贴纸已有另一张图片。请明确是否替换原图，未修改现有贴纸。",
				missing=["replace_image"], missing_labels=["确认替换现有图片"], item_code=existing.name)
		attachment = _attachment_for_item(source, existing) if existing else None
		image_changes = not existing or not attachment or existing.image != source.file_url
		if image_changes:
			if existing:
				existing.check_permission("write")
			if not attachment:
				frappe.has_permission("File", "create", throw=True)
		steps.done("图片文件完整且当前用户有权读取；将保留原附件并关联到贴纸。" if image_changes else "已有贴纸图片完整且可读取，将直接复用。")
		if existing:
			if dry_run:
				return steps.result({"status": "preview", "item_code": existing.name, "item_name": existing.item_name,
					"attributes": target, "image_file": source.name, "image_url": source.file_url,
					"message": "预检通过，将复用现有贴纸；尚未修改物料或图片。"})
			steps.begin("save_attributes", "复用已有贴纸属性。")
			steps.done("属性组合已存在，无需重复登记。", "reused")
			steps.begin("save_item", "复用已有贴纸物料。")
			steps.done("找到唯一且启用的同属性物料，没有重复创建。", "reused")
			steps.begin("save_image", "关联本次图片或复用已有图片。")
			attachment = _save_image(source, existing)
			steps.done("图片已关联，原附件与历史图片保留。" if image_changes else "图片已经正确关联，无需重复保存。", "saved" if image_changes else "reused")
			steps.begin("verify", "回读物料图片字段和图片附件，核验二者关联及私有状态。")
			verified, image = _verify_image(existing, attachment, source)
			steps.done("贴纸与图片关联一致，可直接展示已保存图片。")
			return steps.result(_result(verified, "existing", image=image))

		frappe.has_permission("Item", "create", throw=True)

		definitions, additions, abbreviations = {}, [], {}
		steps.begin("save_attributes", "复用标准贴纸型号，只登记缺少的客户和版本属性值。")
		# Fixed lock order protects shared attribute child tables from lost updates.
		for attribute in sorted(ATTRIBUTES):
			definition = frappe.get_doc("Item Attribute", attribute, for_update=not dry_run)
			definition.check_permission("read")
			if definition.disabled or definition.numeric_values:
				frappe.throw(f"属性“{attribute}”已停用或不支持文本值。")
			definitions[attribute] = definition
			value = target[attribute]
			matches = [row for row in definition.item_attribute_values if _key(row.attribute_value) == _key(value)]
			if len(matches) > 1:
				frappe.throw(f"属性“{attribute}”存在重复值，请先整理。")
			if matches:
				target[attribute] = matches[0].attribute_value
				abbr = matches[0].abbr
				if not abbr or not abbr.strip():
					frappe.throw(f"属性“{attribute}”的现有值缺少缩写，请先完善。")
			else:
				if attribute == "贴纸型号":
					frappe.throw(f"标准贴纸型号“{value}”尚未配置，请管理员同步型号目录。可选型号：{'、'.join(ALLOWED_MODELS)}；客服工具不会新增型号。")
				definition.check_permission("write")
				abbr = value
				if any(_key(row.abbr) == _key(abbr) for row in definition.item_attribute_values):
					frappe.throw(f"属性“{attribute}”的缩写已被其他值占用，请先在物料属性中设置唯一缩写。")
				additions.append({"attribute": attribute, "value": value, "abbr": abbr})
			abbreviations[attribute] = abbr

		# Native generation uses the template's configured order and saved abbreviations.
		code = template.item_code + "-" + "-".join(abbreviations[row.attribute] for row in template.attributes)
		item_name = template.item_name + "-" + "-".join(abbreviations[row.attribute] for row in template.attributes)
		if len(code) > 140 or len(item_name) > 140:
			frappe.throw("生成的物料编码或名称超过 140 字符，请先为属性设置较短且唯一的缩写。")
		if frappe.db.exists("Item", code):
			frappe.throw("生成的物料编码已被不同属性组合占用，请先调整属性缩写。")
		if dry_run:
			steps.done("属性与物料编码检查通过，本次预检未登记属性。", "preview")
			return steps.result({"status": "preview", "item_code": code, "item_name": item_name,
				"variant_of": TEMPLATE, "attributes": target,
				"image_file": source.name, "image_url": source.file_url,
				"pending_attribute_values": additions, "registered_attribute_values": [],
				"message": "预检通过，尚未创建物料、登记属性或关联图片。"})

		for entry in additions:
			definition = definitions[entry["attribute"]]
			touched.append(definition.name)
			definition.append("item_attribute_values", {"attribute_value": entry["value"], "abbr": entry["abbr"]})
			definition.save()
		steps.done("缺少的属性值已登记，已有属性值保持不变。" if additions else "所有属性值已存在，直接复用。", "saved" if additions else "reused")
		steps.begin("save_item", "通过原生变体逻辑创建贴纸物料并写入本次图片路径。")
		variant = create_variant(template.name, target)
		if variant.item_code != code:
			frappe.throw("原生生成的物料编码与预检不一致，已阻止创建。")
		# Avoid inheriting the template's generic image, and do not require an
		# extra Item save solely to set the requested image on a new variant.
		variant.image = source.file_url
		variant.is_stock_item = 1
		variant.insert()
		steps.done("贴纸物料及本次图片路径已保存，继续建立附件关联。", "saved")
		steps.begin("save_image", "为贴纸创建图片附件关联，保留原聊天或档案附件。")
		attachment = _save_image(source, variant)
		steps.done("图片附件已关联到贴纸，原附件与图片私有状态保留。", "saved")
		steps.begin("verify", "回读贴纸属性、图片字段和附件关联，确认保存完整。")
		verified = frappe.get_doc("Item", variant.name)
		verified.check_permission("read")
		actual = [(row.attribute, row.attribute_value) for row in verified.attributes]
		if (verified.variant_of != template.name or verified.disabled or verified.has_variants
			or sorted(actual) != sorted(target.items())):
			frappe.throw("新物料回读核验失败。")
		verified, image = _verify_image(verified, attachment, source)
		steps.done("客户、型号、版本及图片均已保存并核验，可直接展示图片供检查。")
		return steps.result(_result(verified, "created", additions, image))
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		frappe.flags.attribute_values = None
		for name in touched:
			frappe.clear_document_cache("Item Attribute", name)
		if isinstance(exc, NeedsInput):
			return steps.result({"status": "needs_input", "message": str(exc), "rolled_back": True,
				"failed_step": steps.fail(str(exc), rolled_back=True), **exc.details})
		if isinstance(exc, frappe.PermissionError):
			message = f"“{steps.current['label']}”未通过：当前用户缺少本步骤所需的客户、物料、属性或图片附件权限，本次修改已回滚。"
		elif isinstance(exc, frappe.ValidationError):
			message = str(exc)
		else:
			message = f"“{steps.current['label']}”失败（{type(exc).__name__}），本次属性、物料和图片关联已回滚。请联系管理员检查该步骤，不要重复创建。"
		return steps.result({"status": "error", "message": message, "rolled_back": True,
			"error_type": type(exc).__name__, "failed_step": steps.fail(message, rolled_back=True)})
