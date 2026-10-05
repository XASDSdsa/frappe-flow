"""Install read-only inventory, item and supplier selectors owned by Flow."""
import importlib
import inspect

import frappe

from flow.integrations.erpnext.flow_reply_style import with_managed_guidance, with_reply_style
from flow.integrations.erpnext.tool_install import all_enabled, bind_imported_tools, upsert_imported_tools

MODULE = "inventory_flow"
HINT_MARKER = "库存物料采购查询规则："
HINT = (
    HINT_MARKER + (
    '\n'
    '\n'
    '### 只读查询\n'
    '\n'
    '- 库存先用get_inventory_options读取Bin，分别展示实存actual_qty、预留reserved_qty、可用（实存减预留）和projected_qty预计量；不得把预计量当作现有库存，也不得直接改Bin或库存流水。\n'
    '- 不清楚物料时用get_item_options，不清楚供应商时用get_supplier_options；精确编号优先，重名不能取第一条。\n'
    '\n'
    '### 采购入库与限制\n'
    '\n'
    '- 采购收货继续使用get_purchase_receipt_options→preview_purchase_receipt→save_purchase_receipt，只有实际到货且成本已明确才提交入库；用户明确贴纸服务成本已另行入账或免费取得时，沿用该说明传rate=0及zero_valuation_reason，不把未知成本当0、不重复计入服务成本。\n'
    '- 草稿不增加库存，采购收货不会自动付款或开票。\n'
    '- 库存查询是只读动作，不要求客服确认；所有采购写入仍由原生采购单据和一次审核负责。'
)
)


def with_inventory_guidance(instructions):
    text = instructions or ""
    return with_reply_style(with_managed_guidance(text, HINT_MARKER, HINT))


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
    installed = upsert_imported_tools(tool_definitions(), enable=enable)
    agents = bind_imported_tools(installed, enable=enable, guidance=with_inventory_guidance)
    return {"tools": installed, "enabled": all_enabled(installed), "agents": agents}
