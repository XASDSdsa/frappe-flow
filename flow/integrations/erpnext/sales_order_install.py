"""Install the sales order preview and approved draft creation tools."""

import re

import frappe

from flow.integrations.erpnext.flow_reply_style import with_managed_guidance, with_reply_style
from flow.integrations.erpnext.tool_install import bind_imported_tools, upsert_imported_tools


PREVIEW_SLUG = "preview_sales_order"
CREATE_SLUG = "create_sales_order_draft"
DETAILS_SLUG = "query_sales_order_details"
HINT_MARKER = "销售订单专用工具规则："
LEGACY_HINT_MARKER = "创建销售订单的销售团队必填规则："
HINT = (
    HINT_MARKER + """

### 业务顺序与工具选择

- 本规则适用于 Flow、销售助理及继续历史销售订单对话。
- 先确定客户档案，再建立需要的客户贴纸物料，最后创建销售订单；不采用先开单再补客户贴纸档案的流程。
- 客户在外部付款并确认需求后，创建销售订单草稿、审核并提交，再用真实 PayPal 交易号登记到该订单。当前 PayPal 工具要求已提交销售订单，不能伪造无订单预收款。
- 用户要求创建销售订单时，直接调用只读 `preview_sales_order`，沿用本轮已知客户、商品和数量。
- 不先用通用 `find`/`describe` 查询员工、销售人员或子表结构，不向客服索要本人邮箱、账号或销售人员名称。

### 操作者与默认值

- 后端从真实会话核实当前操作者，经原生 `Employee → Sales Person` 关联唯一匹配在职员工和启用销售人员，销售贡献为 **100%**。
- 模型不得猜测操作者；匹配缺失或冲突按后端返回处理，不绕过原生权限。
- 未指定交期时默认 `delivery_days=7`，交期为第 7 天业务时区当日 **23:59**；`stickers_free` 始终为 `true`。
- 未指定发货仓库时默认 **大坪仓库 - LEYA**，审核摘要必须展示并标注可修改；明确其他仓库时传 `warehouse`。

### 商品与贴纸行

- `items` 每行传 `{item_code 或 item_name, qty, uom?, rate?, standalone?, is_free_item?}`。
- 普通商品 `rate` 仅在用户明确指定本单价格时填写；沿用已知资料，不要求重填，不把物料名称中的数字当售价。
- 允许单卖巧克粉，也允许在 `items` 填写客户贴纸独立销售，含仅贴纸订单；不能因缺少配套商品而拒绝或强制补商品。
- 所有客户贴纸，包括配套和独立交付贴纸，销售单价统一为 **0 元**；不得传正价或 `stickers_free=false`。
- 独立贴纸仍必须属于本客户、已启用且管理库存。后端 `warnings` 必须在整体审核卡片醒目展示，提醒核对型号版本、数量和用途，不增加额外确认。

### 定制服务与真实成本

- 本次已确认提供的贴纸定制服务必须列入订单，免费也保留 0 元服务行，不得因未收费而省略。
- 已确认服务传 `customization_services`，每行 `{item_code 或 item_name, qty?, uom?, rate?}`，精确选择已有非库存服务物料；不要再把同一服务重复放入 `items`。
- 服务数量默认 `1`，免费单价默认 `0`；收费时必须传约定单价，不从物料名称中的 500/1000 等数字推断。
- 未提服务不传 `customization_services`；只复用已有贴纸不代表再次定制，不自动新增服务。
- 无法确认服务物料时，一次说明真实缺项，禁止随意取第一个服务物料。
- 复购先问要不要贴纸，并核对库存。库存够用时不主动加制作服务；库存不够或没有对应贴纸时，必须问制作服务，客服确认后才按免费或收费列行。首单不论库存和有没有贴纸档案，都要问制作服务，不得用“复购不收制作费”解释首单。
- 贴纸仍有真实采购成本和库存；服务售价为 0 不代表采购成本为 0，也不重新记入已入账成本。
- 贴纸制作计划或定制服务费不增加库存，必须等实际生产到货后走采购收货。

### 贴纸与制作服务确认

- 预检返回 `first_order_choices` 时还没有批准卡片。不论客服本轮有没有提到贴纸，都先用短表格问完，得到答复后才传 `first_order_confirmed=true`。不得自行把该参数设为 true。
- 首单（`order_kind=first`，或无法判断的 `unknown`）：有没有贴纸档案都要问。1. 要不要贴纸、用哪个版本；没有档案又要贴纸时先建贴纸物料，不出单。2. 要不要制作服务、选哪项、收多少。`services` 带 `reference_prices`，只作提示，金额以客服回答为准。免费传 `rate=0`，不需要就不传 `customization_services`。
- 复购（`order_kind=repeat`）：先问要不要贴纸。表格列出每种贴纸的 `on_hand`（仓库现存量）、`needed_qty`（本单对应颜色数量）和 `enough`。`stock_short=true` 或 `missing_colors` 有值时，库存不够，必须同时问制作服务。库存够用且客服没有要求新做时，不加制作服务。
- 提问保持简短，不附长篇规则说明。客服答复后按回答传 `sticker_mappings` 或 `include_stickers`，再带 `first_order_confirmed=true` 重新预检。批准卡片只显示一行已确认结果。
- 不自动添加未确认的贴纸或服务。

### 商品与贴纸组合

- 不要求固定话术。同单同时提到巧克粉与客户贴纸时，结合上下文、客户、商品型号、颜色、贴纸版本和数量，主动判断对应关系。
- 关系清楚时直接在预检给出建议，整单审核一次；只有多个合理对应或数量不一致时，才集中询问需要澄清的内容。
- 商品和贴纸已分别列在 `items` 时，用 `bundle_mappings=[{product_row:原始items商品行号, sticker_row:原始items贴纸行号}]`，行号从 **1** 开始。
- 已有贴纸行会被组合消费，不再额外新增同一贴纸；后端也会安全匹配唯一型号和实物数量，不能按列表顺序配对。
- 单独交付、备用或不贴在商品上的贴纸，在该 `items` 行传 `standalone=true`，保持独立。
- 没有提到贴纸的商品不添加贴纸、不创建组合。
- 每个组合按 **1 件商品配 1 张贴纸**；数量不同先明确拆分，不擅自增加贴纸或调整原始数量；同一商品配不同贴纸时拆成对应商品行。
- 只给商品并要求对应贴纸时才传 `include_stickers=true`，按每个商品匹配实际数量；仅部分商品需贴纸时只用 `sticker_mappings`，不为其他商品补贴纸。
- 明确指定贴纸时用 `sticker_mappings`，每行 `{product_row:从1开始的商品行号, item_code 或 item_name 或 sticker_model+sticker_version, qty?, uom?, rate?}`。
- `sticker_mappings` 与 `bundle_mappings` 不可重复指定同一商品行；同一贴纸输入行不得重复用于多个组合，不得把整单商品数量套给每种贴纸。
- 匹配有歧义时，一次展示后端候选，仅问实际需要选择的项目。

### 只读预检

- 按原生规则只读计算价格、税费、地址和其他默认值，返回真实 `preview_token` 与订单摘要；不得编造令牌、报价、默认值或保存结果。
- 只按 `missing` 和 `issues` 一次询问真正缺少或错误的资料；已有完整默认值时不追问，不把系统自动字段当成必填问题。
- 客户只差大小写且唯一时，后端已直接认定，不要再查客户档案。多个候选或商品未匹配时，issue 带 `choices`：用表格一次列出让客服选。不要另用 `read`、`find`、客户档案或 `get_item_options` 反复查找。
- 同一订单已有“山东绿方”这类简称时，后面的“蓝方、灰方、粉方”后端按山东中性处理。单独一个“蓝方”仍要按候选选择。
- 预检不保留组合档案；相同商品、同客户贴纸及比例复用组合，不能随每笔订单重复建档。

### 整单审核与一次批准

- 预检准备完成后，提示核对原生批准卡片。完整中文审核摘要在卡片展示一次，聊天不重复整份内容。
- 卡片保留客户、商品明细、配套贴纸、独立销售贴纸、服务、交货与负责人、金额。
- 组合逐行列商品＋贴纸型号版本、数量、商品单价和小计；贴纸标注“免费／0元”，标明组合将复用还是新建。
- 未组合商品和独立贴纸另列，避免重复计算数量或金额。
- 交货显示具体年月日 23:59 前及业务时区，仓库显示完整名称，销售员显示本人姓名及业绩 100%；默认值标注“系统默认，可修改”。
- 服务逐行显示名称、数量、免费或收费及实际金额，免费服务不得隐藏；服务数量不计入商品或贴纸数量。
- 金额显示币种、产品金额、配套贴纸金额、独立贴纸金额、服务金额、已配置税费和订单总额；未核定运费不得虚报已含。
- 自动填写不是免审核，客服须整体核对实际订单摘要；需要修改可拒绝并说明，资料正确后才点批准。
- 紧接着调用 `create_sales_order_draft(preview_token)`，仅保留一次系统原生批准，不要求先回复确认或重复批准。

### 保存、重试与后续业务

- 批准后复用或创建原生销售组合 `Product Bundle` 并创建销售订单草稿；创建组合与订单在同一事务，失败写入统一回滚。
- 组合本身不单独记库存，后续出库按组成商品及贴纸分别管理库存。
- 订单上的非库存服务行默认随第一次出库单按剩余数量带上，提交后回写已交货，不扣库存。不要把服务留到最后一批实物。
- 不提交订单、不扣库存、不生成收款；收款必须在订单提交后由实际 PayPal 或收款工具登记，按实际返回说明。
- 订单提交、贴纸到货入库、出库和物流是后续独立步骤，不能在创建订单时伪造完成。
- 过期或资料变化时重新调用 `preview_sales_order`；重复同一令牌复用原订单，不能据此重新建单。
- 只有用户明确要求第二张相同订单时，才可在预检传 `new_order=true`；不得为重试、继续旧对话或规避重复检查制造新订单意图。
- 原订单已取消或被修改时报告实际状态，不自动重建。

### 查询与结果回复

- 查看销售订单、最新订单或订单明细时，先调用只读 `query_sales_order_details`；按当前权限一次返回完整商品行、组合组件、贴纸资料和收款摘要。
- 明细原样展示返回的 `detail_table` 表格：每条订单商品一行，组合拆成商品列和贴纸列，贴纸写明客户 · 型号 · 版本；不重复列组合名和组件，不能只写“巧克粉+贴纸”，也不改写成文字段落。
- 表格前只写一行订单号、客户、状态和交期，表格后只写一行订单金额、已收和未收；用户要看贴纸图片时，用对应贴纸的 `sticker.image` 调用 `show_image`。
- 不先用通用 `read` 读取子表，不改用 `get_delivery_note_options`、`get_sales_invoice_options` 或 `execute`；销售订单查询与出库、开票是不同动作。
- 仅 `verified=true` 的成功结果可报告保存成功，并提供回读核实后的真实订单链接。
- 正文用短摘要说明当前结果、实际异常原因和下一步；完整 `steps` 不默认逐条复述，准确区分预检通过、待批准、已保存、已复用、失败与已回滚，不编造过程或链接。"""
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
    text = instructions or ""
    text = _without_legacy_guidance(text)
    return with_reply_style(with_managed_guidance(text, HINT_MARKER, HINT))


TOOLS = (
        (
            DETAILS_SLUG, "读取销售订单完整明细", False,
            """只读返回当前账号可见的销售订单完整明细和收款摘要。

### 用途

- 查询指定销售订单、最新订单或订单明细。

### 参数与默认值

- `sales_order`：精确销售订单号；未传时返回最新未取消订单。
- `customer`：未传订单号时，可按客户编号筛选。

### 返回与下一步

- 一次返回完整商品行、数量、仓库、金额和收款摘要。
- 组合商品带 `components`（巧克粉、贴纸等组成及数量）；客户贴纸带 `sticker`（客户、型号、版本、图片）。
- `detail_table` 是现成的 Markdown 明细表，直接原样展示。

### 限制

- 遵守当前账号读取权限；不读取出库单或发票，不执行代码，不保存任何记录。""",
        ),
        (
            PREVIEW_SLUG, "销售订单只读预检", False,
            """只读核对销售订单资料与默认值，返回完整审核摘要及真实预检令牌。

### 用途

- 按当前真实会话操作者与原生销售人员关联，核对客户、商品、可选对应贴纸、价格、税费、地址与交期。

### 参数与默认值

- `items` 每行使用 `{item_code 或 item_name, qty, uom?, rate?, standalone?, is_free_item?}`；所有客户贴纸单价 **0 元**。
- 已确认定制服务传 `customization_services=[{item_code 或 item_name, qty?, uom?, rate?}]`；仅用已有非库存服务物料，不再重复放入 `items`。
- 服务默认 `qty=1`、免费 `rate=0`，免费也保留服务行；收费传明确单价。未提服务或复用旧贴纸不新增服务。
- 同单商品与贴纸按上下文主动判断组合；已有贴纸行用 `bundle_mappings=[{product_row:原始商品行号, sticker_row:原始贴纸行号}]`，均从 **1** 开始。
- 独立交付或备用贴纸标 `standalone=true`；允许仅贴纸订单。组合按每件商品配一张贴纸，关系或数量有歧义才集中询问。
- 用户只说配对应贴纸时可传 `include_stickers=true`；明确指定时用 `sticker_mappings`，每行 `{product_row:从1开始, item_code 或 sticker_model+sticker_version, qty?}`。
- 默认仓库为 **大坪仓库 - LEYA**，交期为 **7 天后当天 23:59 前**；可用 `warehouse`/`delivery_date` 修改，均须展示审核。
- `first_order_confirmed`：首单贴纸和制作服务已由客服确认时传 `true`，默认 `false`。

### 返回与下一步

- 返回真正缺项、歧义候选、完整订单摘要及 `preview_token`；客户或商品未匹配时 issue 带 `choices` 候选。
- 确认前一律返回 `first_order_choices`，不出令牌。首单问要不要贴纸、要不要制作服务。复购问要不要贴纸，并给出 `on_hand`、`needed_qty`、`enough`；`stock_short` 时同时问制作服务。客服答复后传 `first_order_confirmed=true` 才出令牌。
- 无权读取历史订单时返回 `sticker_service_review` 说明，随整单审核。
- 已有订单按复购处理，沿用已确认贴纸版本；库存不足只提示后续必须补货，不将订单误报为可发货。
- 组合在一次整体批准后复用或创建，不另外建组合库存。

### 限制

- 未提贴纸不新增；本人销售员自动识别，不接受代填操作者。
- 预检不保留组合档案或订单。""",
        ),
        (
            CREATE_SLUG, "批准并创建销售订单草稿", True,
            """一次原生批准后创建并回读核验销售订单草稿，失败完整回滚。

### 用途

- 批准后复核预检、权限与默认值，再创建销售订单草稿。

### 参数与默认值

- `preview_token`：仅使用 `preview_sales_order` 返回的真实令牌。

### 返回与下一步

- 创建并回读核验草稿；失败完整回滚。
- 重复令牌复用原订单；过期或资料变化时重新预检。

### 限制

- 不提交订单、不扣库存、不登记收款，不重新创建已取消或已修改的原订单。""",
        ),
    )


def install_sales_order_tools(enable=False):
	"""Refresh tool definitions; only explicit enable activates and binds them."""
	tool_definitions = [
		{
			"slug": slug,
			"title": title,
			"requires_confirmation": confirm,
			"description": description,
			"summary": (
				"一次批准后保存并核验销售订单草稿。" if confirm else
				"只读返回销售订单完整商品明细和收款摘要。" if slug == DETAILS_SLUG else
				"只读计算订单与缺项，返回真实预检摘要及令牌。"
			),
			"import_path": "flow.integrations.erpnext.sales_order_flow." + slug,
		}
		for slug, title, confirm, description in TOOLS
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
		"description": TOOLS[0][3],
		"summary": "只读返回销售订单完整商品明细和收款摘要。",
		"import_path": "flow.integrations.erpnext.sales_order_flow." + DETAILS_SLUG,
	}
	if not frappe.db.exists("DocType", "Flow Tool"):
		return {"installed": False, "reason": "Flow 尚未安装"}
	name = upsert_imported_tools([definition], enable=enable)[0]
	agents = bind_imported_tools([name], enable=enable, agent_titles=("Flow", "销售助理"))
	return {"installed": True, "tool": name, "enabled": bool(frappe.db.get_value("Flow Tool", name, "enabled")), "agents": agents}
