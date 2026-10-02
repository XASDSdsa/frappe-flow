"""Install read-only inventory, item and supplier selectors owned by Flow."""
import importlib
import inspect
import re

import frappe

from flow.integrations.erpnext.flow_reply_style import with_reply_style

MODULE = "inventory_flow"
HINT_MARKER = "库存物料采购查询规则："
HINT = (
    HINT_MARKER +
    "库存先用get_inventory_options读取Bin，分别展示实存actual_qty、预留reserved_qty、"
    "可用（实存减预留）和projected_qty预计量；不得把预计量当作现有库存，也不得直接改Bin或库存流水。"
    "不清楚物料时用get_item_options，不清楚供应商时用get_supplier_options；精确编号优先，重名不能取第一条。"
    "采购收货继续使用get_purchase_receipt_options→preview_purchase_receipt→save_purchase_receipt，"
    "只有实际到货且成本已明确才提交入库；用户明确贴纸服务成本已另行入账或免费取得时，"
    "沿用该说明传rate=0及zero_valuation_reason，不把未知成本当0、不重复计入服务成本。"
    "草稿不增加库存，采购收货不会自动付款或开票。"
    "库存查询是只读动作，不要求客服确认；所有采购写入仍由原生采购单据和一次审核负责。"
)


def with_inventory_guidance(instructions):
    text = re.sub(re.escape(HINT_MARKER) + r"[^\r\n]*(?:\r?\n)*", "", instructions or "")
    return with_reply_style((text.rstrip() + "\n\n" + HINT).lstrip("\n"))


def tool_definitions():
    module_object = importlib.import_module("flow.integrations.erpnext." + MODULE)
    result = []
    for slug, title, requires_confirmation, description in module_object.TOOLS:
        function = getattr(module_object, slug)
        details = getattr(function, "description", None) or inspect.getdoc(function) or ""
        result.append({"slug": slug, "title": title,
            "requires_confirmation": int(requires_confirmation),
            "description": description + ("\n\n" + details if details else ""),
            "import_path": "flow.integrations.erpnext." + MODULE + "." + slug})
    return result


def install_inventory_tools(enable=False):
    if not frappe.db.exists("DocType", "Flow Tool"):
        return {"installed": False, "reason": "Flow 尚未安装"}
    installed = []
    for definition in tool_definitions():
        values = {**definition, "type": "Imported", "code": None,
                  "summary": definition["title"]}
        if enable:
            values["enabled"] = 1
        name = frappe.db.get_value("Flow Tool", {"slug": definition["slug"]}, "name")
        if name:
            frappe.db.set_value("Flow Tool", name, values)
        else:
            doc = frappe.get_doc({"doctype": "Flow Tool", "enabled": int(enable), **values})
            doc.insert(ignore_permissions=True)
            name = doc.name
        frappe.clear_document_cache("Flow Tool", name)
        installed.append(name)
    agents = []
    if enable and frappe.db.exists("DocType", "Flow Agent"):
        for title in ("Flow", "销售助理"):
            name = frappe.db.get_value("Flow Agent", {"title": title}, "name")
            if not name:
                continue
            agent = frappe.get_doc("Flow Agent", name)
            changed = False
            for tool_name in installed:
                if not any(row.tool == tool_name for row in agent.get("tools") or []):
                    agent.append("tools", {"tool": tool_name})
                    changed = True
            instructions = with_inventory_guidance(agent.get("instructions"))
            if instructions != (agent.get("instructions") or ""):
                agent.instructions = instructions
                changed = True
            if changed:
                agent.save(ignore_permissions=True, ignore_version=True)
            frappe.clear_document_cache("Flow Agent", name)
            agents.append(name)
    return {"tools": installed, "enabled": enable, "agents": agents}
