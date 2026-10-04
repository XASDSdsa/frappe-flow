"""Install the customer sticker variant tool without enabling server scripts."""

import frappe

from flow.integrations.erpnext.flow_reply_style import with_managed_guidance, with_reply_style
from .image_guidance import SHOW_IMAGE_HINT, SHOW_IMAGE_HINT_EXPLICIT_ONLY

TOOL_SLUG = "create_customer_sticker_variant"
TOOL_IMPORT_PATH = "flow.integrations.erpnext.sticker_variant.create_customer_sticker_variant"
HINT_MARKER = "巧克粉贴纸专用工具："
HINT = (
	HINT_MARKER + """

### 资料与型号

- 创建客户巧克粉贴纸变体时调用 `create_customer_sticker_variant`；提供系统中明确的客户编号或完整客户名称、贴纸型号、贴纸版本和图片 `image_file`。
- `sticker_model` 只能使用 **山东中性方形、广东油性方形、圆形**；禁止新增其他型号、修改型号目录或新建总模板。
- 结合客服自然语言、所配巧克粉和本轮已明确的产品信息匹配：山东中性／山东方形 → 山东中性方形；广东油性／广东方形 → 广东油性方形；圆形贴纸 → 圆形。
- 旧名称中的“模板”“模版”不构成新型号。能唯一匹配时直接写入审核摘要，不再让客服选择。
- 只说方形、同时涉及多种型号或无法唯一匹配时，简短列出三个标准选项，只补问缺少的型号依据，不能猜测。
- 沿用本轮已明确或刚建立的客户、已提供的型号和版本、本次上传的图片 File 编号，不重复询问已知资料。
- 缺少客户、型号、版本或必要图片时，一次列齐真正缺项并等待补齐，不提前调用写入工具；不追问手机号、地址、币种等客户建档字段。

### 图片与替换

- `image_file` 可用已知的 File 编号或本站图片路径，不得猜测编号或路径；多张候选图片未指定用途时，只问使用哪张。
- 新建必须有图片；已有贴纸可复用档案中已登记的可用图片，不要求重传。
- 已有另一张图片时先说明冲突，不得静默覆盖；只有用户明确选择替换才传 `replace_image=true`，正式操作仍仅一次原生批准。

### 审核与执行

- 资料齐全先简短列出客户、型号、版本和拟登记图片，说明批准后自动创建并登记图片，紧接着调用正式工具，保留一次原生批准卡片。
- 不要求先回复确认，不逐步询问是否登记属性、绑定图片或查看图片。
- 一次完成查重、必要客户与版本属性登记、原生变体创建、图片关联和回读核验；不拆成 `execute`、`create`、`update` 多次操作，不要求开启服务器脚本。
- 只有用户要求预览时使用 `dry_run=true`；正式创建保留一次工具确认。

### 结果与展示

- `created` 表示新建成功，`existing` 表示复用已有物料。
- `existing_disabled` 表示已存在但停用，不能报告为可用或自动启用。
- `needs_input` 只补问缺失或有歧义的信息；`preview` 不代表已创建；`error` 按返回信息说明，不绕过权限或盲目重建。
- 只有 `verified=true` 且成功结果含 `image_file`/`image_url` 时，立即调用一次 `show_image(file=返回的image_file)` 展示已登记图片，无需用户再要求看图或批准。
- 图片由 `show_image` 自动展示，不额外输出图片 Markdown 或重复调用；展示失败时明确“档案已保存但图片预览失败”，不能因此重建档案。
- 完成后用一行绿色状态说明贴纸已创建或复用、图片已登记，并写“请核对下方图片；正确则无需再操作”。
- 正文只保留结果、实际异常原因和真实物料链接；完整 `steps` 不默认逐条复述，不重复物料编码和名称，不追加无关的换图、补充资料或再次确认问题。

### 库存与成本

- 客户贴纸必须是维护库存的物料；本工具只建立档案与图片，不生成库存。
- 完成后简短注明：待实际到货后按真实数量和成本采购入库；未入库不能出库，免费赠送也要记录库存成本。"""
)


def with_sticker_guidance(instructions):
	text = instructions or ""
	text = text.replace(SHOW_IMAGE_HINT_EXPLICIT_ONLY, SHOW_IMAGE_HINT)
	return with_reply_style(with_managed_guidance(text, HINT_MARKER, HINT))


TOOL_DESCRIPTION = """按客户、贴纸型号和贴纸版本创建巧克粉贴纸物料变体并登记图片。

### 用途

- 先核对权限和模板、完整查重，再按 ERPNext 原生变体规则继承模板并生成编码。
- 在同一事务关联图片、写入物料图片字段并回读核验，保留原聊天附件。

### 参数与默认值

- `customer_name`：明确的客户编号或完整客户名称。
- `sticker_model`、`sticker_version` 必填；新建还需 `image_file`，使用已上传图片的 File 编号或本站图片路径。已有完整档案可复用原图。
- `sticker_model` 仅允许山东中性方形、广东油性方形、圆形；先将自然语言唯一匹配为标准名称，方形等含糊描述须补齐依据。
- `dry_run=true` 只预检；正式创建需要一次确认。
- 不同原图需用户明确替换才传 `replace_image=true`，不会静默覆盖。

### 返回与下一步

- 成功后直接调用 `show_image` 展示返回图片，供客服核对。
- 失败回滚本次属性、物料和附件写入。

### 限制

- 资料不齐先补齐，不发起写入批准。
- 新增时只追加缺少的客户和版本属性值；型号必须复用三个已配置标准值，不新增型号或总模板。
- 不会启用停用物料；无需通用 `execute` 或开启服务器脚本。"""


def install_sticker_tool(enable=False):
	"""Deploy implementation; explicit enable also adds the two chat entry points."""
	if not frappe.db.exists("DocType", "Flow Tool"):
		frappe.throw("Flow 尚未安装")
	values = {
		"type": "Imported", "import_path": TOOL_IMPORT_PATH, "code": None,
		"title": "创建巧克粉贴纸变体", "requires_confirmation": 1,
		"description": TOOL_DESCRIPTION,
		"summary": "客户＋型号＋版本＋图片，一次批准完成建档和图片登记，完成后直接展示图片核对。",
	}
	if enable:
		values["enabled"] = 1
	name = frappe.db.get_value("Flow Tool", {"slug": TOOL_SLUG}, "name")
	if name:
		frappe.db.set_value("Flow Tool", name, values)
	else:
		doc = frappe.get_doc({"doctype": "Flow Tool", "slug": TOOL_SLUG, "enabled": int(enable), **values})
		doc.insert(ignore_permissions=True)
		name = doc.name
	frappe.clear_document_cache("Flow Tool", name)
	agents = []
	for agent_name in frappe.get_all("Flow Agent", pluck="name"):
		agent = frappe.get_doc("Flow Agent", agent_name)
		bound = any(row.tool == name for row in agent.get("tools") or [])
		if not bound and not (enable and agent_name in ("Flow", "销售助理")):
			continue
		changed = False
		if not bound:
			agent.append("tools", {"tool": name})
			changed = True
		instructions = with_sticker_guidance(agent.get("instructions"))
		if instructions != (agent.get("instructions") or ""):
			agent.instructions = instructions
			changed = True
		if changed:
			agent.save(ignore_permissions=True, ignore_version=True)
		frappe.clear_document_cache("Flow Agent", agent_name)
		agents.append(agent_name)
	return {"tool": name, "type": "Imported", "enabled": bool(frappe.db.get_value("Flow Tool", name, "enabled")),
		"requires_confirmation": True, "agents": agents}
