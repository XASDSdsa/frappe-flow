"""Register Flow's reviewed native Sales Order and Delivery Note submission."""

import frappe

from flow.integrations.erpnext.flow_reply_style import with_managed_guidance, with_reply_style

HINT_MARKER = "原生单据提交专用工具规则："
HINT = HINT_MARKER + (
    '\n'
    '\n'
    '### 用途与参数\n'
    '\n'
    '- 销售订单或出库单创建为草稿后，客服要提交时先调用 preview_document_submission；只允许传真实 doctype（Sales Order 或 Delivery Note）和原生单据编号，不创建新单、不修改商品、不调用通用 execute。\n'
    '\n'
    '### 审核与提交\n'
    '\n'
    '- 预检会按当前登录客服和原生权限读取客户、商品、数量、仓库、金额和单据版本；资料会在审核卡完整展示。\n'
    '- 审核卡正确后只批准一次 submit_reviewed_document；执行时锁定原单并核对客服、当前对话、修改时间、单据内容和提交权限。\n'
    '- 审核后单据变化就返回需重新审核，不调用原生提交；取消单拒绝；已经提交的单据只回读当前状态，不重复提交。\n'
    '\n'
    '### 结果与限制\n'
    '\n'
    '- 销售订单提交不扣库存；出库单提交严格执行 ERPNext 原生库存、会计和仓库校验。\n'
    '- 提交失败保留真实原因和当前状态，不把草稿说成已提交。\n'
    '- 成功必须返回真实单号、状态和入口；未 verified=true 不得报告提交完成。'
)

TOOLS = (
	("preview_document_submission", "预检原生销售订单或出库单提交", False,
	 (
        '只读预检现有销售订单或出库单的提交。\n'
        '\n'
        '### 参数与默认值\n'
        '\n'
        '- `doctype` 仅支持 `Sales Order`、`Delivery Note`；`document` 为真实原生单据编号。\n'
        '\n'
        '### 返回与下一步\n'
        '\n'
        '- 核对客户、商品、数量、仓库、金额、当前版本和提交权限，返回真实 `submission_token`，随后调用 `submit_reviewed_document`。\n'
        '\n'
        '### 限制\n'
        '\n'
        '- 不保存、不提交、不新建单据。'
    )),
	("submit_reviewed_document", "批准并提交原生销售订单或出库单", True,
	 (
        '整体批准后按原生规则提交现有销售订单或出库单。\n'
        '\n'
        '### 参数与默认值\n'
        '\n'
        '- 使用 `preview_document_submission` 返回的真实 `submission_token`。\n'
        '\n'
        '### 返回与下一步\n'
        '\n'
        '- 批准一次后锁定原单并提交；版本变化需重新审核；已提交只回读当前状态。\n'
        '- 仅 `verified=true` 时报告提交完成，并给出真实单号、状态和入口。\n'
        '\n'
        '### 限制\n'
        '\n'
        '- 核对当前客服、对话、单据版本和提交权限；取消单拒绝，不重复提交。\n'
        '- 销售订单提交不扣库存；出库单执行原生库存、会计和仓库校验。'
    )),
)


def with_document_submission_guidance(instructions):
	text = instructions or ""
	return with_reply_style(with_managed_guidance(text, HINT_MARKER, HINT))


def install_document_submission_tools(enable=False):
	if not frappe.db.exists("DocType", "Flow Tool"):
		frappe.throw("Flow 尚未安装")
	installed = []
	for slug, title, confirm, description in TOOLS:
		values = {"type": "Imported", "code": None, "title": title,
			"description": description, "summary": title,
			"requires_confirmation": int(confirm),
			"import_path": "flow.integrations.erpnext.document_submission." + slug}
		if enable:
			values["enabled"] = 1
		name = frappe.db.get_value("Flow Tool", {"slug": slug}, "name")
		if name:
			frappe.db.set_value("Flow Tool", name, values)
		else:
			doc = frappe.get_doc({"doctype": "Flow Tool", "slug": slug, "enabled": int(enable), **values})
			doc.insert(ignore_permissions=True)
			name = doc.name
		frappe.clear_document_cache("Flow Tool", name)
		installed.append(name)
	agents = []
	if enable and frappe.db.exists("DocType", "Flow Agent"):
		for title in ("Flow", "销售助理"):
			name = frappe.db.get_value("Flow Agent", {"title": title}, "name") or (title if frappe.db.exists("Flow Agent", title) else None)
			if not name or name in agents:
				continue
			agent = frappe.get_doc("Flow Agent", name)
			changed = False
			for tool_name in installed:
				if not any(row.tool == tool_name for row in agent.get("tools") or []):
					agent.append("tools", {"tool": tool_name})
					changed = True
			instructions = with_document_submission_guidance(agent.get("instructions"))
			if instructions != (agent.get("instructions") or ""):
				agent.instructions = instructions
				changed = True
			if changed:
				agent.save(ignore_permissions=True, ignore_version=True)
			frappe.clear_document_cache("Flow Agent", name)
			agents.append(name)
	return {"tools": installed, "type": "Imported", "enabled": all(bool(frappe.db.get_value("Flow Tool", n, "enabled")) for n in installed), "agents": agents}
