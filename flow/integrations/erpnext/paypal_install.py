"""Targeted installation for the PayPal receipt tool and its reservation table."""

import frappe


TOOL_SLUG = "paypal_receipt_procedure"
TOOL_IMPORT_PATH = "flow.integrations.erpnext.paypal_receipt.paypal_receipt_procedure"
TOOL_TITLE = "登记 PayPal 美元收款"
TOOL_DESCRIPTION = (
	(
    '登记外部已确认到账的 PayPal 美元销售订单预收款。\n'
    '\n'
    '### 参数与默认值\n'
    '\n'
    '- `sales_order` 为尚未开票的销售订单；传真实 `transaction_id`、`posting_date`、`reference_date`。\n'
    '- `gross` 为收款总额、`fee` 为 PayPal 手续费、`net` 为净到账，均为 USD；`currency=USD`，`customer` 可选。\n'
    '- `dry_run=false`；传 `true` 只读预检，正式登记须用户确认。\n'
    '\n'
    '### 返回与下一步\n'
    '\n'
    '- 相同交易号和相同收款内容重复请求返回原凭证；冲突信息阻止登记。\n'
    '- 已开票订单使用原生销售发票收款流程。\n'
    '- 返回中的实际收款币种始终为 USD；公司本位币字段仅表示 ERP 总账折算。客服必须分开展示“实际 PayPal 收款：USD”和“总账折算：CNY（仅内部核算，不代表收到 CNY，也没有换汇）”。\n'
    '\n'
    '### 限制\n'
    '\n'
    '- 本工具不调用 PayPal 接口验证到账，不办理提现或结汇。'
)
)


def install_paypal_tool():
	"""Load only the PayPal schema and replace the existing tool implementation.

	The caller owns the transaction. Agent bindings, slug, enabled state and all
	unrelated tools are preserved.
	"""
	if not frappe.db.exists("DocType", "Flow Tool"):
		frappe.throw("Flow 尚未安装，无法更新 PayPal 收款工具。")

	if not frappe.db.exists("DocType", "PayPal Receipt Record"):
		frappe.throw("ERPNext 的 PayPal 收款记录尚未安装。")
	values = {
		"type": "Imported",
		"import_path": TOOL_IMPORT_PATH,
		"code": None,
		"title": TOOL_TITLE,
		"description": TOOL_DESCRIPTION,
		"summary": "登记外部已确认的 PayPal 美元订单预收款；支持只读预检和防重复入账。",
		"requires_confirmation": 1,
	}
	tool_name = frappe.db.get_value("Flow Tool", {"slug": TOOL_SLUG}, "name")
	if tool_name:
		# A deployment changes a Script tool to Imported without renaming the row
		# or touching bindings. Retaining its enabled value also retains opt-outs.
		frappe.db.set_value("Flow Tool", tool_name, values)
		frappe.clear_document_cache("Flow Tool", tool_name)
	else:
		doc = frappe.get_doc({"doctype": "Flow Tool", "slug": TOOL_SLUG, "enabled": 1, **values})
		doc.insert(ignore_permissions=True)
		tool_name = doc.name

	return {
		"tool": tool_name,
		"type": "Imported",
		"import_path": TOOL_IMPORT_PATH,
		"requires_confirmation": 1,
	}
