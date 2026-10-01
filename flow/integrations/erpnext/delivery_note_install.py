"""Register the explicit-selection Delivery Note workflow as Imported tools."""
import re

import frappe
from flow.integrations.erpnext.flow_reply_style import with_reply_style

HINT_MARKER = "出库单专用工具规则："
HINT = (
    HINT_MARKER + "本流程只创建出库单草稿，完整顺序是已提交销售订单和有效收款→列出待出库行→客服选择→库存与原生校验→创建草稿→提交出库单→再进入物流。"
    "用户说‘销售订单准备出库’、‘订单出库’或‘准备发货’且没有已存在的出库单号/顺丰运单号时，必须先调用get_delivery_note_options；"
    "禁止先调用get_sf_shipment_status、query_sf_tracking、query_sf_freight或book_sf_waybill，因为销售订单本身不是顺丰运单。"
    "客服明确要求查询已有物流时才使用状态/轨迹工具，可按订单号查已有运单；不把没有运单当作不能创建出库单的理由。"
    "客户提出出库需求时，先调用get_delivery_note_options，已知订单号直接传sales_order；仅有客户时传准确客户编号customer。"
    "有多个订单须明确本次订单，不能按第一个候选或最新日期擅自选单。"
    "工具核对订单存在、本人可读、已提交且有效、尚有剩余出库数量，以及有效收款。"
    "全款和部分收款都可进入出库；必须展示订单金额、已收金额和未收余额，部分收款用橙色提示且由客服整体审核批准；"
    "完全未收款停止，不虚报到账；零金额订单注明无需收款。混合发票分配不清或币种无法核实时说明具体原因，不能猜金额。"
    "先把订单商品（包括订单已有贴纸）用三列表格列给客服：行号、名称、本次可出库数量及单位；不默认铺开全部数量列。"
    "有草稿占用、退货或多个仓库时，用短句明确受影响行及数量/仓库；需要核对原数量时再展示订单数量和已出库数量。"
    "有退货时说明已出库为扣除退货后的净数量，不声称从未发货；已有草稿返回可读链接供处理，无权限的草稿不泄露单号。"
    "展示明细后必须停下来等客服回答出库哪些商品和数量，或明确全部出库；此阶段不调用创建工具、不自行默认全选。"
    "选品后调用preview_delivery_note(selection_token,items=[{row_no或sales_order_item,qty}])；只有明确全部出库才能传all_remaining=true。"
    "客服明确某几行全部出库时使用这些行展示的可出库数量，无需重复问同一数量；只报商品且数量含糊时一次补问。"
    "相同商品编码可能对应多行，按表格行号区分，不能合并猜测。未选择的免费贴纸不能自动添加；全部出库包含本次剩余贴纸。"
    "客服看到明细前或尚未回复选择时，后端会拒绝进入预检。数量、收款或草稿状态变化后重新列明细并选择。"
    "预检通过后提示核对原生批准卡片，完整审核保留订单/客户、收款提示、本次商品及数量、继承的订单仓库、收货地址、组合包组件和本次金额，聊天不重复整份摘要。"
    "紧接着调用create_delivery_note_draft(preview_token)，由系统显示真实摘要并整体批准一次，不另要求先回复确认再批准。"
    "拒绝时按修改意见重新预检；不能伪造令牌或用通用create/脚本绕过专用工具，也不询问销售团队或子表独立读取权限。"
    "保存前会重新核对并防止相同方案重复创建。重复请求返回原单，不另开；异常写入完整回滚。"
    "保留原订单客户、币种、价格、销售团队及仓库，不把当前出库客服改成订单销售员。"
    "客户专属贴纸必须维护库存；出库草稿保存和提交都按指定仓库检查实际库存，包含组合包贴纸和同物料多行合计，不能负库存出库。已按原生规则入账的零成本库存可以出库，不重复计入已记账服务成本。巧克粉及其他非贴纸物料沿用当前原生库存规则，不新增缺货限制。"
    "订单待出库数量不等于仓库实有库存；缺贴纸时简短说明具体贴纸、仓库、本次需要、可用和缺少数量，先完成真实采购到货入库，或由客服明确减少本次数量，不能悄悄减量或删掉贴纸。复购订单沿用原客户贴纸版本，只有实际库存足够才允许继续创建和提交出库。"
    "贴纸建档、付款、采购订单和图片登记均不增加库存；到货按实际合格数量和真实成本完成原生采购入库，禁止虚构收货、成本或自动勾选允许零估值来放行。已出现状态/库存/记账不一致的单据交管理员修复，不能重复提交或绕过异常继续创建面单。"
    "只verified=true时报告成功及真实出库单链接；正文只写当前结果、实际异常和下一步，完整steps不默认逐条复述。"
    "创建草稿不等于已出库或已发货，不提交、不扣库存、不创建运单、不调用顺丰；提交成功后才可进入通用运单和具体物流服务商流程，取得单号也不等于包裹已经交给物流。"
)


TOOLS = (
    ("get_delivery_note_options", "列出待出库订单和商品", False,
     "只读核对已提交有效订单、实际收款和剩余数量。已知订单传sales_order；仅有客户传customer。先向客服展示商品行号、数量、单位及草稿占用，等待明确选品或全部出库，不默认全选。"),
    ("preview_delivery_note", "预检客服选定的出库明细", False,
     "必须使用真实selection_token，且客服已看到明细并回复选品。items逐行传{row_no或sales_order_item,qty}；明确全部出库才传all_remaining=true。只读预检并返回完整摘要和preview_token。"),
    ("create_delivery_note_draft", "批准并创建出库单草稿", True,
     "使用真实preview_token，经一次整体批准后复核收款、数量及草稿占用，按原生订单转换保存出库草稿并回读。失败整体回滚，相同令牌复用原单。不提交、不扣库存、不创建物流运单。"),
)


def with_delivery_note_guidance(instructions):
    text = re.sub(re.escape(HINT_MARKER) + r"[^\r\n]*(?:\r?\n)*", "", instructions or "")
    return with_reply_style((text.rstrip() + "\n\n" + HINT).lstrip("\n"))


def install_delivery_note_tools(enable=False):
    if not frappe.db.exists("DocType", "Flow Tool"):
        frappe.throw("Flow 尚未安装")
    installed = []
    for slug, title, confirm, description in TOOLS:
        values = {"type": "Imported", "code": None, "title": title, "description": description,
                  "summary": title, "requires_confirmation": int(confirm),
                  "import_path": "flow.integrations.erpnext.delivery_note_flow." + slug}
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
            instructions = with_delivery_note_guidance(agent.get("instructions"))
            if instructions != (agent.get("instructions") or ""):
                agent.instructions = instructions
                changed = True
            if changed:
                agent.save(ignore_permissions=True, ignore_version=True)
            frappe.clear_document_cache("Flow Agent", name)
            agents.append(name)
    return {"tools": installed, "type": "Imported", "enabled": all(bool(frappe.db.get_value("Flow Tool", n, "enabled")) for n in installed), "agents": agents}
