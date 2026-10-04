"""Install the sales order preview and approved draft creation tools."""

import re

import frappe

from flow.integrations.erpnext.flow_reply_style import with_reply_style
from flow.integrations.erpnext.tool_install import bind_imported_tools, upsert_imported_tools


PREVIEW_SLUG = "preview_sales_order"
CREATE_SLUG = "create_sales_order_draft"
DETAILS_SLUG = "query_sales_order_details"
HINT_MARKER = "销售订单专用工具规则："
LEGACY_HINT_MARKER = "创建销售订单的销售团队必填规则："
HINT = (
    HINT_MARKER + "本规则适用于 Flow、销售助理及继续历史销售订单对话。"
    "业务顺序为先确定客户档案，再建立需要的客户贴纸物料，最后创建销售订单；客户在外部付款并确认需求后创建销售订单草稿、审核并提交，再用真实 PayPal 交易号登记到该订单。当前 PayPal 工具要求已提交销售订单，不能伪造无订单预收款；不采用先开单再补客户贴纸档案的流程。"
    "用户要求创建销售订单时直接调用只读 preview_sales_order，沿用本轮已知客户、商品和数量；"
    "不得先用通用 find/describe 查询员工、销售人员或子表结构，不向客服索要本人邮箱、账号或销售人员名称。"
    "实际当前操作者由后端从会话核实，以原生 Employee→Sales Person 关联唯一匹配在职员工和启用销售人员，销售贡献为 100%；"
    "不得由模型猜测操作者，匹配缺失或冲突按后端返回处理，不绕过原生权限。"
    "items 每行传 {item_code 或 item_name, qty, uom?, rate?, standalone?, is_free_item?}；普通商品 rate 只在用户明确指定本单价格时填写，沿用已知资料，不要求用户重填。免费定制服务行必须是非库存服务物料，并同时传 is_free_item=true、rate=0；不能把物料名称中的数字当售价。"
    "允许单卖巧克粉，也允许直接在 items 填写客户贴纸独立销售，含仅贴纸订单；不得仅因缺少配套商品而拒绝或强制补商品。"
    "所有客户贴纸，包括配套和独立交付贴纸，销售单价统一为0元；不得传正价或stickers_free=false。"
    "贴纸仍有真实采购成本和库存；定制服务费只有客户明确同意收费时才另列服务行，金额必须按本单约定传入，不能从物料名称推断。若服务费由已记账的贴纸投入承担，本单可将非库存服务物料明确标记为is_free_item=true并传rate=0；普通库存商品不得这样处理。贴纸制作计划或定制服务费不增加库存，必须等实际生产到货后走采购收货。"
    "预检返回sticker_service_review时，整体审核卡片会醒目展示首单贴纸服务选择；按返回的历史可见范围说明，不擅称全系统首单。"
    "已选贴纸核对商品对应、型号版本和数量；未选贴纸核对需要或不需要，需要时补齐贴纸物料后重新预检。"
    "用户已明确选择的不要重复询问；首单提醒不增加单独确认，不强制购买贴纸服务，不自动添加贴纸或定制费。复购同一客户贴纸版本时直接复用版本和已确认成本，不再重复添加定制服务费；只有新版本或新一批确有实际投入时才作为新的定制事项审核。"
    "独立贴纸仍必须属于本客户、已启用且管理库存；后端 warnings 必须在整体审核卡片醒目展示，提醒核对型号版本、数量和用途，不增加额外确认。"
    "不要求固定话术：同一订单同时提到巧克粉与客户贴纸，就结合上下文、客户、商品型号、颜色、贴纸版本和数量主动判断组合对应关系。"
    "关系清楚时直接在预检中给出建议，整单审核一次；只有仍有多个合理对应或数量不一致时才集中问需要澄清的内容。"
    "商品和贴纸已分别列在items时，用bundle_mappings=[{product_row:原始items商品行号,sticker_row:原始items贴纸行号}]，行号从1开始；"
    "已有贴纸行会被组合消费，不再额外新增同一贴纸。后端也会安全匹配唯一的型号和实物数量，不能把列表顺序当作配对依据。"
    "明确单独交付、备用或不贴在商品上的贴纸，给该items行standalone=true，保持独立；没有提到贴纸的商品不添加贴纸、不创建组合。"
    "每个组合按1件商品配1张贴纸；数量不同先明确拆分，不擅自增加贴纸或调整原始数量。同一商品配不同贴纸时拆成对应商品行。"
    "只给商品并要求对应贴纸时才传include_stickers=true，按每个商品匹配实际数量；部分商品需贴纸时只用sticker_mappings，不为其他商品补贴纸。"
    "用户明确指定贴纸时使用 sticker_mappings，每行传 {product_row:从1开始的商品行号, item_code 或 item_name 或 sticker_model+sticker_version, qty?, uom?, rate?}；"
    "sticker_mappings与bundle_mappings不可重复指定同一商品行；同一贴纸输入行也不得重复用于多个组合。不得把整单商品数量套给每种贴纸。"
    "匹配有歧义时一次展示后端候选，仅问实际需要选择的项目。"
    "未指定交期时由后端默认 delivery_days=7，交期为第7天业务时区当日23:59；stickers_free始终为true。"
    "未指定发货仓库时默认大坪仓库 - LEYA，审核摘要必须展示并标注可修改；明确其他仓库时通过 warehouse 传入。"
    "预检按原生规则只读计算价格、税费、地址及其他默认值，返回真实 preview_token 与订单摘要；不得编造令牌、报价、默认值或保存结果。"
    "只按返回 missing 和 issues 一次询问真正缺少或错误的资料；已有完整默认值时不追问，不把系统自动字段当成必填问题。"
    "预检准备完成后提示核对原生批准卡片，完整中文审核摘要在卡片展示一次，聊天不重复整份内容；卡片保留客户、商品明细、配套贴纸、独立销售贴纸、交货与负责人、金额："
    "组合逐行列商品+贴纸型号版本、数量、商品单价和小计，贴纸明确标注‘免费／0元’，标明组合将复用还是新建；未组合商品和独立贴纸另列，避免重复计算数量或金额；"
    "交货显示具体年月日23:59前和业务时区，仓库显示完整名称，销售员显示本人姓名及业绩100%，默认值标注‘系统默认，可修改’；"
    "金额显示币种、产品金额、配套贴纸金额、独立贴纸金额、已配置税费和订单总额，未核定运费不得虚报已含；商品数量、配套贴纸数量和独立贴纸数量分开，不把所有数量混称为商品数量。"
    "自动填写不是免审核，客服必须整体核对上述实际订单摘要；需要修改可拒绝并说明，资料正确后才点批准。"
    "紧接着调用 create_sales_order_draft(preview_token)，仅保留一次系统原生批准，不要求先回复确认或重复批准。"
    "正式工具批准后复用或创建原生销售组合Product Bundle并创建销售订单草稿；组合本身不单独记库存，后续出库按组成商品及贴纸分别管理库存。"
    "预检不保留组合档案；相同商品、同客户贴纸及比例复用组合，不能随每笔订单重复建档；创建组合与订单在同一事务，失败写入统一回滚。"
    "不提交订单、不扣库存、不生成收款；收款必须在订单提交后由实际 PayPal 或收款工具登记，按实际返回说明。订单提交、贴纸到货入库、出库和物流均是后续独立步骤，不能在创建订单时伪造完成。"
    "过期或资料已变化的预检必须重新调用 preview_sales_order；重复同一令牌复用原订单，不能据此重新建单。"
    "只有用户明确要求第二张相同订单时才可在预检传 new_order=true，不得为重试、继续旧对话或规避重复检查制造新订单意图。"
    "用户要求查看销售订单、最新订单或订单明细时，先调用只读 query_sales_order_details；它会按当前权限一次返回完整商品行和收款摘要。"
    "不要先用通用 read 读取子表，也不要改用 get_delivery_note_options、get_sales_invoice_options 或 execute；销售订单查询与出库、开票是不同动作。"
    "原订单已取消或被修改时报告实际状态，不自动重建。仅 verified=true 的成功结果可报告保存成功，并提供回读核实后的真实订单链接。"
    "正文只用短摘要说明当前结果、实际异常原因和下一步；完整steps不默认逐条复述，仍准确区别预检通过、待批准、已保存、已复用、失败与已回滚，不编造过程或链接。"
)


def _without_legacy_guidance(text):
    """Remove the old heading and its numbered paragraphs, keeping other text."""
    lines = text.splitlines(keepends=True)
    kept = []
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith(LEGACY_HINT_MARKER):
            kept.append(lines[index])
            index += 1
            continue
        index += 1
        for number in range(1, 4):
            start = index
            while start < len(lines) and not lines[start].strip():
                start += 1
            if start >= len(lines) or not re.match(rf"^[ \t]*{number}[.．、)）][ \t]*", lines[start]):
                break
            index = start + 1
            # Wrapped lines belong to this paragraph only when indented. An
            # unrelated unindented paragraph must remain untouched.
            while index < len(lines) and lines[index].strip() and lines[index][0] in " \t":
                if re.match(r"^[ \t]*\d+[.．、)）]", lines[index]):
                    break
                index += 1
    return "".join(kept)


def with_sales_order_guidance(instructions):
    """Replace managed guidance and retire the former sales-team instructions."""
    text = _without_legacy_guidance(instructions or "")
    text = re.sub(re.escape(HINT_MARKER) + r"[^\r\n]*(?:\r?\n)*", "", text)
    return with_reply_style((text.rstrip() + "\n\n" + HINT).lstrip("\n"))


def install_sales_order_tools(enable=False):
	"""Refresh tool definitions; only explicit enable activates and binds them."""
	definitions = (
        (
            DETAILS_SLUG, "读取销售订单完整明细", False,
            "只读读取当前账号可见的指定销售订单；未传订单号时返回最新未取消订单。一次返回完整商品行、数量、仓库、金额和收款摘要。"
            "不读取出库单或发票，不执行代码，不保存任何记录。",
        ),
        (
            PREVIEW_SLUG, "销售订单只读预检", False,
            "按当前真实会话操作者与原生销售人员关联，只读核对客户、商品、可选对应贴纸、价格、税费、地址与交期。"
            "items 每行使用 {item_code 或 item_name, qty, uom?, rate?, standalone?, is_free_item?}；所有客户贴纸单价0元；收费定制服务按明确金额另列，已由投入承担的免费服务行使用非库存服务物料、is_free_item=true、rate=0。"
            "同单商品与贴纸按上下文主动判断组合，已有贴纸行用bundle_mappings=[{product_row:原始商品行号,sticker_row:原始贴纸行号}]，均从1开始。"
            "明确独立交付或备用贴纸标standalone=true；允许仅贴纸订单。每件商品配一张贴纸，关系或数量有歧义才集中询问。"
            "用户只说配对应贴纸时可include_stickers=true；sticker_mappings每行{product_row:从1开始,item_code或sticker_model+sticker_version,qty?}。"
            "未提贴纸不新增；组合在一次整体批准后复用或创建，不另外建组合库存。本人销售员自动识别，不接受代填操作者。"
            "默认大坪仓库 - LEYA、7天后当天23:59前，可用warehouse/delivery_date修改，均须展示审核。"
            "未查到当前账号可见的未取消历史订单时返回首单贴纸服务提醒sticker_service_review，随整单审核；已有订单时按复购处理并沿用已确认贴纸版本，库存不足只提示后续必须补货，不把订单误报为可发货；无读取权限会明确说明。"
            "返回真正缺项、歧义候选、完整订单摘要及 preview_token，不保留组合档案或订单。",
        ),
        (
            CREATE_SLUG, "批准并创建销售订单草稿", True,
            "仅使用 preview_sales_order 返回的真实 preview_token，在一次原生批准后复核预检、权限与默认值，"
            "创建并回读核验销售订单草稿；失败完整回滚。重复令牌复用原订单；过期或有变化需重新预检。"
            "不提交订单、不扣库存、不登记收款，不重新创建已取消或已修改的原订单。",
        ),
    )
	tool_definitions = [
		{
			"slug": slug,
			"title": title,
			"requires_confirmation": confirm,
			"description": description,
			"summary": "一次批准后保存并核验销售订单草稿。" if confirm else "只读计算订单与缺项，返回真实预检摘要及令牌。",
			"import_path": "flow.integrations.erpnext.sales_order_flow." + slug,
		}
		for slug, title, confirm, description in definitions
	]
	installed = upsert_imported_tools(tool_definitions, enable=enable)
	agents = bind_imported_tools(
		installed,
		enable=enable,
		agent_titles=("Flow", "销售助理"),
		guidance=with_sales_order_guidance,
	)
	enabled = [bool(frappe.db.get_value("Flow Tool", name, "enabled")) for name in installed]
	return {"tools": installed, "type": "Imported", "enabled": all(enabled), "agents": agents}


def install_sales_order_query_tool(enable=True):
	"""将历史订单明细脚本迁移为 Flow 源码工具并绑定两个业务 Agent。"""
	definition = {
		"slug": DETAILS_SLUG,
		"title": "读取销售订单完整明细",
		"requires_confirmation": False,
		"description": "只读读取当前账号可见的指定销售订单；未传订单号时返回最新未取消订单。一次返回完整商品行、数量、仓库、金额和收款摘要。"
		"不读取出库单或发票，不执行代码，不保存任何记录。",
		"summary": "只读返回销售订单完整商品明细和收款摘要。",
		"import_path": "flow.integrations.erpnext.sales_order_flow." + DETAILS_SLUG,
	}
	if not frappe.db.exists("DocType", "Flow Tool"):
		return {"installed": False, "reason": "Flow 尚未安装"}
	name = upsert_imported_tools([definition], enable=enable)[0]
	agents = bind_imported_tools([name], enable=enable, agent_titles=("Flow", "销售助理"))
	return {"installed": True, "tool": name, "enabled": bool(frappe.db.get_value("Flow Tool", name, "enabled")), "agents": agents}
