"""Register the reviewed SF-label workflow as Imported Flow tools."""
import re
import frappe
from flow.integrations.erpnext.flow_reply_style import with_reply_style

HINT_MARKER = "顺丰面单专用工具规则："
HINT = (
    HINT_MARKER + "从已提交的出库单创建首次顺丰面单时只使用本流程，不自动提交出库单。"
    "旧顺丰直接写入口（包括book_sf_waybill、直接打印、直接发货、直接取消和旧重建）均已停用并解绑，旧对话也不能再次调用；先准备地址候选，再预检和整体批准create_sf_label。"
    "用户只说销售订单准备出库时，不调用本流程，也不调用顺丰状态查询；先用出库单 Imported 工具核对并创建出库单。"
    "先调用prepare_sf_label(delivery_note)，核对已提交有效出库单及已有运单。已有单号或下单处理中只查询原结果，不重复创建。"
    "已有草稿会沿用；多张运单、合并出库或异常状态先说明具体阻塞原因，不自行选择。"
    "历史成功地址优先展示，必须醒目标注‘🟢 历史成功下单地址 · 优先推荐 · 仍需客服选择’。"
    "历史地址只有国家、邮编和完整街道与当前档案相同才可使用，姓名电话仍取当前档案，不能复制历史联系人。"
    "仅地址记忆没有可读历史单证明时标注橙色核对提示。没有可用历史时自动调用顺丰邮编反查。"
    "客服要求重新查询时传lookup_postcode=true。AI只能在工具实际返回的候选中筛选、排序和推荐，不能编造省市或候选编号。"
    "候选用编号+国家/省州/城市/区县/邮编+来源展示；候选地区与档案不同须醒目标注具体差异，不能悄悄改成档案文字。"
    "只显示推荐的少量候选时说明总数和筛选理由；需要其他候选调用list_sf_label_addresses分页读取。"
    "即使只找到一个历史地址也必须先展示并停下来等客服选择；AI不得替客服默认选中。"
    "同步列出真实缺项，尤其每种包裹的长宽高厘米、单件重量千克和件数；同一次回复让客服选地址并补齐缺项，已知资料不要重复询问。"
    "不得从商品净重推测包裹毛重，不得编造尺寸或使用0.1千克默认重量。"
    "客服选择后调用preview_sf_label(preparation_token,address_choice_id,parcels,product_code,receiver_details,customs)。"
    "省市只能由真实候选编号带入，receiver_details仅补联系人、电话、邮箱、公司、门牌；改街道邮编须先修正档案重新准备。"
    "保留已有顺丰产品与系统申报预设并清楚标注来源，不另行要求每个默认值单独确认；实际申报信息由客服整体核对。"
    "预检通过后提示核对原生批准卡片，其中完整保留客户、出库单、本次商品数量、发件仓库/发件人、完整收件地址/电话、包裹尺寸件数总重、顺丰产品、申报金额币种HS编码中英文品名；聊天不重复整份摘要。"
    "接着调用create_sf_label(preview_token)，使用系统一次完整批准；不要先要求回复确认再点批准，也不要每一步都批准。"
    "批准后保存并提交运单，后台请求顺丰并生成PDF，不等于已发货。queued只说已排队，processing只说处理中；有真实waybill才说单号已创建。"
    "取得真实单号和面单后，客服确认包裹已实际交给物流时才调用dispatch_sf_label；该工具一次审核，读取现有面单并标记已发货，已发货重复调用只返回原状态，不重复打印或请求顺丰。"
    "调用get_sf_label_result只读查看结果，同一次回复最多检查两次，仍处理中就提供原运单链接，不循环空等或重复创建。"
    "ready展示真实单号和打开面单链接，请客服核对打印；created_without_pdf说明已有单号但PDF未就绪或失败原因，从原运单打印，绝不重新下单。"
    "uncertain表示结果待核对，明确具体原因和保留原记录，不承诺下单失败、更不能自动重试。needs_review说明批准后哪项变化、当前未发送请求，需核对原运单。"
    "每次只用短摘要说明当前结果、实际异常原因和下一步；完整steps保留在工具结果中，不默认逐条复述，未执行不能写成完成。"
    "常规查询、准备、核对不要求批准；仅地址选择和整体批准需要客服操作，真实缺项一次补齐。"
    "不得通过通用创建、脚本或另一创建工具绕过本流程的选择、审批与重复下单防护。"
    "查询当前或历史面单先用get_sf_shipment_status，用户给了具体顺丰单号就传waybill，并始终保留该单号。"
    "历史换单记录必须明确标注当前面单或历史面单；只读查询物流query_sf_tracking和运费query_sf_freight继续传同一waybill，不能悄悄换成当前单号。"
    "用户未指定旧号且有多张历史面单时，展示真实候选后再选择，不能默认最新一条就是所需历史单。"
    "query_sf_tracking只读返回顺丰和已保存历史，不写入；需要把最新轨迹保存回系统时才调用sync_sf_tracking并整体批准。历史面单的运费也独立核对，不能遗漏或覆盖当前面单费用。查询运费可能按现有规则记账，仍保留原生批准。"
)
TOOLS = (
    ("prepare_sf_label", "准备顺丰面单并列出地址候选", False,
     "先核对已提交出库单，优先推荐匹配当前街道邮编的历史地区，无历史自动顺丰邮编反查。展示候选编号并等客服选择；同步补齐包裹资料。返回preparation_token。"),
    ("list_sf_label_addresses", "查看更多顺丰地址候选", False,
     "用preparation_token和start分页读取真实地址候选，不创建面单、不替客服选地址。"),
    ("preview_sf_label", "预检顺丰面单并生成审核摘要", False,
     "客服明确选择地址后传真实preparation_token和address_choice_id，parcels为长宽高厘米/单件重量千克/件数。核对全部下单参数，返回preview_token，不实际下单。"),
    ("create_sf_label", "批准并创建顺丰面单", True,
     "用真实preview_token，经一次整体批准后保存并提交运单，提交事务后后台向顺丰下单并生成PDF。返回排队状态不等于成功；不提交出库单，不标记实际发货，不重复下单。"),
    ("get_sf_label_result", "查看顺丰面单实际结果", False,
     "用真实shipment编号只读查看原下单任务、真实顺丰单号和已保存PDF。不能触发重下或再次打印；结果不明需核对原运单。"),
    ("dispatch_sf_label", "确认已打印面单并发货", True,
     "客服确认包裹已交给物流后整体批准；读取现有顺丰面单并按原生规则标记已发货，已发货重复调用只返回原状态，不重复下单。"),
)


def with_sf_label_guidance(instructions):
    text = re.sub(re.escape(HINT_MARKER) + r"[^\r\n]*(?:\r?\n)*", "", instructions or "")
    return with_reply_style((text.rstrip() + "\n\n" + HINT).lstrip("\n"))


def install_sf_label_tools(enable=False):
    """Refresh existing registrations; explicit enable activates/binds the tools."""
    if not frappe.db.exists("DocType", "Flow Tool"):
        frappe.throw("Flow 尚未安装")
    retire_legacy_booking_tool()
    installed = []
    for slug, title, confirm, description in TOOLS:
        values = {"type": "Imported", "code": None, "title": title, "description": description,
            "summary": title, "requires_confirmation": int(confirm),
            "import_path": "flow.integrations.erpnext.sf_label_flow." + slug}
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
            for tool_name in installed:
                if not any(row.tool == tool_name for row in agent.get("tools") or []):
                    agent.append("tools", {"tool": tool_name})
            agent.instructions = with_sf_label_guidance(agent.get("instructions"))
            agent.save(ignore_permissions=True)
            frappe.clear_document_cache("Flow Agent", name)
            agents.append(name)
    return {"tools": installed, "type": "Imported", "enabled": all(bool(frappe.db.get_value("Flow Tool", n, "enabled")) for n in installed), "agents": agents}


def retire_legacy_booking_tool():
    """Retain legacy SF tool identities for audit; disable and unbind every live entry.

    The callable implementations remain available to native Shipment/waybill code,
    but none of these direct write tools may be exposed to Flow.  Keeping the rows
    (instead of deleting them) preserves audit/history and makes repeated installs
    idempotent.
    """
    if not frappe.db.exists("DocType", "Flow Tool"):
        return {"disabled": [], "agents": []}
    legacy_slugs = [
        "book_sf_waybill",
        "print_sf_label",
        "dispatch_sf_shipment",
        "cancel_sf_waybill",
        "recreate_sf_waybill",
    ]
    rows = frappe.get_all("Flow Tool", filters={"slug": ["in", legacy_slugs]}, pluck="name")
    for name in rows:
        frappe.db.set_value("Flow Tool", name, {
            "enabled": 0,
            "title": "旧顺丰入口（已停用）",
            "description": "此旧入口已停用，不能直接写入顺丰运单。请按准备地址、预检和整体批准流程操作。",
        })
        frappe.clear_document_cache("Flow Tool", name)
    changed = []
    if rows and frappe.db.exists("DocType", "Flow Agent"):
        names = set(rows)
        for name in frappe.get_all("Flow Agent", pluck="name"):
            doc = frappe.get_doc("Flow Agent", name)
            kept = [row for row in doc.get("tools") or [] if row.tool not in names]
            if len(kept) != len(doc.get("tools") or []):
                doc.set("tools", kept)
                doc.save(ignore_permissions=True)
                frappe.clear_document_cache("Flow Agent", name)
                changed.append(name)
    return {"disabled": rows, "agents": changed}
