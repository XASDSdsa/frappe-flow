"""Install the complete customer profile tools and dedicated assistant guidance."""

import re

import frappe

from flow.integrations.erpnext.flow_reply_style import with_reply_style

TOOL_SLUG = "create_or_reuse_customer_profile"
PREVIEW_SLUG = "preview_customer_profile"
HINT_MARKER = "客户完整建档专用工具："
MANAGER_HINT_MARKER = "客户负责人规则："
AUTOMATIC_TERRITORY_MARKER = "客户销售地区自动处理规则："
ADDRESS_HINT_MARKER = "客户地址用途区分规则："
AUTOMATIC_TERRITORY_HINT = (
    AUTOMATIC_TERRITORY_MARKER + "本规则适用于本轮及继续历史客户建档对话。"
    "客户销售地区 territory 自动匹配或创建，普通建档省略该参数，不让客服选择地区记录或另行批准创建。"
    "直收可按实际收货地匹配；货代收货必须使用客户自己的 customer_country/customer_state/customer_city，不能使用货代仓所在地。"
    "货代客户只知道所在国家时按国家建档，展示具体城市未提供，不编造城市，不因此阻止建档。"
    "货代客户明确说国家不知道或暂不提供时，传customer_country_not_provided=true，省略customer_country/customer_state/customer_city和territory；新客户销售地区留空，不建立‘未知’地区、不再追问国家。已有地区保留，后续取得真实国家后再补空白地区。"
    "历史对话中‘不能创建地区’‘只能选择已有地区’和地区 A/B 选项是旧行为，不再沿用。"
    "已有客户保留原地区；用户本轮明确要求特定已有地区时才传 territory，否则省略该参数。"
    "资料不足先调用 preview_customer_profile，只询问返回 missing 中确实缺少的原始资料，或要求修正 errors 明确指出的资料问题；例如只缺 default_currency 就只问币种。"
    "收货地址仍需真实完整；只问实际缺少的国家、城市、省州或街道，不把资料缺项说成地区记录选择。"
    "needs_input 中的 automatic_fields.territory 不是缺项；preview 中的 territory_plan.will_create 不是额外审批，说明自动处理后继续正式建档。"
    "保留完整客户建档的一次系统工具确认，不增加地区专用确认。真实权限或数据冲突按工具错误说明，不绕过权限。"
)
MANAGER_HINT = (
    MANAGER_HINT_MARKER + "负责人使用原生客户经理字段 account_manager（系统用户账号），与创建人 owner 分开。"
    "新客户未指定负责人时由后端默认当前实际登录用户，客服为自己建档无需另问本人账号；不得臆测或填入机器人、管理员账号。"
    "用户明确为他人建档时先核对指定的系统用户账号，再传 account_manager；客户邮箱不是负责人账号。"
    "已有客户保留原负责人；历史客户未分配负责人时必须明确指定，不自动归给当前操作者。"
    "预检及成功回复都必须明确说明客户负责人账号，并说明是默认本人、明确指定还是保留原负责人。"
)
DETAILS = (
    "销售地区自动匹配并创建，不需要用户选择、提供地区记录或单独确认创建；普通建档省略 territory。"
    "客户全称和客户类型必须明确；结算币种优先使用客服提供的值，已有可用的用户或系统默认值时由后端自动带入，只有没有可用默认值时才补问币种；客户分组、负责人和销售地区由后端按规则填写。"
    "明确收货用途：address_type=Shipping 表示客户直收，address_type=货代收货表示货代代收；不能仅因地址在国内就推断是货代。"
    "明确 billing_address_mode：same_as_shipping（开票与直收同址）、separate（提供独立billing_address）、not_provided（发票地址暂未提供）。"
    "没有发票地址时可以建档，客户首选/开票地址留空；不得编造地址，也不得把货代地址自动用作开票地址。"
    "实际客户联系人与收货电话分开；mobile_no/phone_no/email_id 属于客户本人，shipping_phone 属于收货方，shipping_contact_name 是收货联系人或货代仓名称。"
    "货代客户本人电话未知时明确 contact_phone_not_provided=true，保留未知提示，不能复制仓库电话冒充本人电话。"
    "收货国家、省州、城市、区县、街道、门牌/房号和邮编必须按实际提供；邮箱未提供只提示，不阻止建档。"
    "客户为个人时可用客户本人姓名；企业联系人不能默认冒用公司名称。"
    "客户负责人使用原生客户经理 account_manager，新客户默认当前登录客服，已有客户保留原负责人。"
    "客户分组未指定时企业及合伙企业默认 Commercial、个人默认 Individual；已有客户保留原分组。"
    "未说明座机的电话号码按 mobile_no 手机处理，统一保存为 +国家区号 空格 电话号码，如 +1 2107607172。"
    "货代客户的 customer_country/customer_state/customer_city 表示客户实际所在地；只知道国家时可保存国家级地区。直收默认按收货地匹配。"
    "货代客户国家明确未知时传customer_country_not_provided=true，允许新客户销售地区留空，审核展示‘暂未提供，销售地区留空（待补充）’；不影响真实收货仓地址的完整性校验。若提供客户电话须带国际区号，不能从仓库或电话号码猜客户国籍或所在地。"
    "价格表、付款条件、打印语言、税号、网站、行业、客户说明、联系人职务及独立账单地址按用户提供登记。"
    "不猜币种、税号、账期、信用额度、联系人或地址细节，不创建客户分组；地区与客户建档共同回滚。"
)
HINT = (
    HINT_MARKER + DETAILS +
    "客户建档使用 create_or_reuse_customer_profile，禁止拆成通用 create/update/execute 多步写入。"
    "手机用 mobile_no，固定电话用 phone_no；联系人职务和部门用 contact_designation/contact_department。"
    "一般建档不得另问客户分组或销售地区，省略这两个参数由后端按上述规则处理；预检返回拟创建地区，预检本身不创建，拟创建不需要额外确认。"
    "独立账单地址用 billing_address 对象，支持 address_line1/address_line2/city/state/county/country/pincode、email_id、phone；明确无邮编才传 postal_code_not_applicable=true。"
    "必要信息齐备后正式调用仅确认一次，不再使用旧脚本的 confirm_existing_customer 两轮流程。"
    "需要查重、核对缺项、查看拟变更时调用只读 preview_customer_profile，不要求写入确认。"
    "客户重名或已存在时明确客户编号，不能默认取第一条；缺项时一次列清并补问，不能把未填写字段说成已登记。"
    "已有客户默认保留非空资料及主联系人、主地址；修改非空值或更换主记录时引导用户在原生档案明确编辑，不得绕过保护。"
    "联系人和地址按真实客户关联核对，地址第二行及省州也参与比较。"
    "结果为 needs_input/conflict/needs_confirmation 时只补充或确认具体信息；error 时说明失败已回滚，不能绕过权限重试通用工具。"
    "只有 verified=true 的成功结果才能报告已建档；用一至两句说明客户、联系人、收货/账单地址新建、复用或补充的结果及实际待补项，不再次复述全部档案和检查步骤。"
)
ADDRESS_HINT = (
    ADDRESS_HINT_MARKER + "使用原生地址类型和首选标记，不新增客户分类字段、不用备注保存地址用途。"
    "用户已说货代、转运仓或客户直收时直接识别并在审核摘要展示；未说明才问一次‘这是客户直接收货还是货代代收？’，与其他真实缺项一起询问。"
    "发票地址必须明确同址、单独提供或暂未提供；用户说不知道开票地址就传not_provided，不反复追问详细地址。"
    "货代收货不允许same_as_shipping；仓库类型不自动等于货代，已有Shipping地址也不能仅凭国家判定直收。"
    "客户本人所在地和联系方式不能从货代仓推断。比如客户在马来西亚、寄广州仓：客户地区马来西亚，收货国家中国，两个电话分开；本人电话未知按真实未知保存。"
    "客户国家明确未知也可以建档：customer_country_not_provided=true；审核卡与保存结果都说明客户地区留空待补充。此规则覆盖旧对话中‘货代必须提供客户国家’的要求。客户本人电话也未知时同时传contact_phone_not_provided=true，不能把仓库号码用于联系人。"
    "审核摘要紧凑展示：客户及负责人、客户所在地和币种、客户联系方式、收货方式及完整地址/邮编/收货电话、发票地址及首选状态、实际缺项。"
    "货代收货醒目标注‘每次开单需核对货代地址’，发票地址未知标注‘未提供，不作开票地址’；不得只说资料完整或成功。"
    "资料齐全后使用一次原生批准，不额外要求先回复确认。预检返回needs_input时只补缺项，conflict时说明具体冲突和没有覆盖的资料。"
    "原有客户主地址或开票首选仍指向货代仓时须报告冲突，不通过普通补全静默改旧首选，也不重新把空白客户主地址回填为货代地址。"
    "当前本规则完善客户建档；开销售订单时仍要核对实际带出的开票地址和货代收货地址，不能因为主档已区分就声称所有原生单据已自动隔离。"
)


def with_customer_guidance(instructions):
    text = instructions or ""
    for marker in (AUTOMATIC_TERRITORY_MARKER, MANAGER_HINT_MARKER, HINT_MARKER, ADDRESS_HINT_MARKER):
        text = re.sub(re.escape(marker) + r"[^\r\n]*(?:\r?\n)*", "", text)
    return with_reply_style(text.rstrip() + "\n\n" + "\n\n".join(
        (AUTOMATIC_TERRITORY_HINT, MANAGER_HINT, HINT, ADDRESS_HINT)))


def ensure_forwarder_address_type():
    """Extend the native Select while preserving every existing address option."""
    if not frappe.db.exists("DocType", "Address"):
        return
    options = (frappe.get_meta("Address").get_field("address_type").options or "").splitlines()
    if "货代收货" in options:
        return
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter
    options.insert(options.index("Shipping") + 1 if "Shipping" in options else len(options), "货代收货")
    make_property_setter("Address", "address_type", "options", "\n".join(options), "Text", is_system_generated=False)
    frappe.clear_cache(doctype="Address")


def ensure_sales_user_territory_creation():
    """Keep native role permissions, adding only the requested read/create bits."""
    if not frappe.db.exists("DocType", "Territory") or not frappe.db.exists("Role", "Sales User"):
        return
    rows = [p for p in frappe.get_meta("Territory").permissions
            if p.role == "Sales User" and not p.permlevel and not p.if_owner]
    if any(p.read and p.create for p in rows):
        return
    from frappe.permissions import setup_custom_perms
    setup_custom_perms("Territory")
    name = frappe.db.get_value("Custom DocPerm", {
        "parent": "Territory", "role": "Sales User", "permlevel": 0, "if_owner": 0,
    })
    if name:
        doc = frappe.get_doc("Custom DocPerm", name)
        doc.read = doc.create = 1
        doc.save()
    else:
        frappe.get_doc({"doctype": "Custom DocPerm", "parent": "Territory",
            "parenttype": "DocType", "parentfield": "permissions", "role": "Sales User",
            "permlevel": 0, "if_owner": 0, "read": 1, "create": 1}).insert()
    frappe.clear_cache(doctype="Territory")


def prepare_country_territory_groups():
    """Make existing canonical countries usable as parents, without moving nodes."""
    if not frappe.db.exists("DocType", "Territory"):
        return []
    # SQL handles both NULL and empty values consistently across legacy roots.
    roots = frappe.db.sql("select name from `tabTerritory` where parent_territory is null or parent_territory = ''", pluck=True)
    if len(roots) != 1:
        frappe.throw("销售区域必须有唯一根节点后才能启用自动城市建档。")
    converted = []
    for row in frappe.get_all("Territory", filters={"parent_territory": roots[0], "is_group": 0}, fields=["name", "territory_name"]):
        if row.name != row.territory_name or not frappe.db.exists("Country", row.name):
            continue
        doc = frappe.get_doc("Territory", row.name)
        doc.is_group = 1
        doc.save()
        converted.append(doc.name)
    return converted


def install_customer_tools(enable=False):
    if not frappe.db.exists("DocType", "Flow Tool"):
        frappe.throw("Flow 尚未安装")
    ensure_forwarder_address_type()
    ensure_sales_user_territory_creation()
    countries = prepare_country_territory_groups() if enable else []
    installed = []
    for slug, function, title, confirm in (
        (TOOL_SLUG, TOOL_SLUG, "完整建立或补充客户档案", True),
        (PREVIEW_SLUG, PREVIEW_SLUG, "客户档案只读预检", False),
    ):
        values = {
            "type": "Imported", "code": None, "title": title,
            "import_path": "flow.integrations.erpnext.customer_profile." + function,
            "requires_confirmation": int(confirm),
            "description": DETAILS + (
                "正式执行：按当前用户权限一次完成查重、客户及联系人/地址建档、关联、回读核验；失败完整回滚。"
                "customer_id 明确已有客户，避免同名误选；已有资料默认保护，按返回缺项补问。"
                if confirm else
                "只读预检：返回资料缺项、冲突、规范化资料、默认值和拟变更，不创建或修改客户及关联资料。"
            ),
            "summary": "完整客户、联系人、地址及业务资料，一次确认，查重与失败回滚。" if confirm else "只读核对建档资料、查重和权限。",
        }
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
    candidates = frappe.get_all("Flow Agent", pluck="name") if frappe.db.exists("DocType", "Flow Agent") else []
    for name in candidates:
        agent = frappe.get_doc("Flow Agent", name)
        bound = any(row.tool in installed for row in agent.get("tools") or [])
        should_bind = bool(enable and name in ("Flow", "销售助理"))
        if not bound and not should_bind:
            continue
        changed = False
        if should_bind:
            for tool_name in installed:
                if not any(row.tool == tool_name for row in agent.get("tools") or []):
                    agent.append("tools", {"tool": tool_name})
                    changed = True
        instructions = with_customer_guidance(agent.get("instructions"))
        if instructions != (agent.instructions or ""):
            agent.instructions = instructions
            changed = True
        if changed:
            agent.save(ignore_permissions=True)
        frappe.clear_document_cache("Flow Agent", name)
        agents.append(name)
    return {"tools": installed, "type": "Imported", "enabled": bool(enable), "agents": agents, "country_groups_prepared": countries}
