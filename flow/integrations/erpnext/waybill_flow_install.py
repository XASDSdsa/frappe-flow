"""Register the reviewed SF replacement-waybill Flow tools."""

from flow.integrations.erpnext.flow_reply_style import with_managed_guidance, with_reply_style
from flow.integrations.erpnext.tool_install import all_enabled, bind_imported_tools, upsert_imported_tools

HINT_MARKER = "顺丰换单专用工具规则："
HINT = HINT_MARKER + (
    '\n'
    '\n'
    '### 预检与审核\n'
    '\n'
    '- 旧单号和历史运费必须保留。\n'
    '- 需要换单时先调用preview_sf_waybill_replacement，把旧单号、换单原因、新寄件/收件/货物参数和未发货/已发货场景列成审核摘要。\n'
    '\n'
    '### 未发货与已发货处理\n'
    '\n'
    '- 未发货只能在原面单已有明确取消凭证后创建新单；已发货可以先创建新面单，再到顺丰外部客服确认。\n'
    '- 未发货创建成功后系统会自动启用新单，但不等于已发货；已发货创建成功后保持待替换，须调用record_sf_waybill_replacement_feedback记录顺丰外部客服反馈，只有反馈为确认成功后，才可调用activate_sf_waybill_replacement。\n'
    '- 不能把本系统客服或自由文本当作顺丰确认。\n'
    '\n'
    '### 结果与限制\n'
    '\n'
    '- 任何失败、处理中或待核实状态都保留原记录，不能重复下单或偷偷切换当前单号。'
)


def with_replacement_guidance(instructions):
	text = instructions or ""
	return with_reply_style(with_managed_guidance(text, HINT_MARKER, HINT))


def install_waybill_replacement_tools(enable=False):
	from .waybill_flow import TOOLS

	installed = upsert_imported_tools([
		{"slug": slug, "title": title, "requires_confirmation": confirm, "description": description,
		 "summary": title, "import_path": "flow.integrations.erpnext.waybill_flow." + slug}
		for slug, title, confirm, description in TOOLS
	], enable=enable)
	agents = bind_imported_tools(installed, enable=enable, guidance=with_replacement_guidance)
	return {"tools": installed, "type": "Imported", "enabled": all_enabled(installed), "agents": agents}
