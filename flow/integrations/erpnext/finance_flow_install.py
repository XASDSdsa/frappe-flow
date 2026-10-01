"""Install native procurement/invoicing tools without role changes."""
import importlib
import inspect
import re

import frappe
from flow.integrations.erpnext.flow_reply_style import with_reply_style

MODULES = ('purchase_order_flow', 'purchase_receipt_flow', 'sales_invoice_flow')
HINT_MARKER = '采购开票工具规则：'
LEGACY_HINT_MARKER = '采购开票与贴纸利润工具规则：'
HINT = (HINT_MARKER + '业务顺序固定为客户档案、客户贴纸物料、客户收款或预收款、销售订单、订单提交，再按实际贴纸到货办理采购入库，库存满足后出库和物流；不能为了出库伪造采购成本或到货。'
    '采购先用preview_purchase_order→save_purchase_order创建采购订单；到货再用get_purchase_receipt_options→preview_purchase_receipt→save_purchase_receipt；开票用get_sales_invoice_options→preview_sales_invoice→save_sales_invoice。'
    '缺项集中询问一次，沿用本轮已知资料，不重复要求账号；默认仓库大坪仓库 - LEYA并在审核卡列出。'
    '采购必须真实供应商、数量和采购价，成本不从售价或客户定制费猜测；只有用户明确实际已到货并要求提交才用actual_receipt=true、submit=true。'
    '用户明确贴纸服务成本已另行入账、本次须零成本入库，或明确免费取得时，采购收货可传rate=0及zero_valuation_reason，原因沿用用户说明，不重复追问，不把未知成本当0。'
    '零成本只增加实际库存数量，不重复计入已记账服务成本；在原生整单审核卡醒目展示原因和数量，无需另一次确认。复购同一贴纸版本不重复添加定制服务费，只有新一批确有实际成本时才新增一次费用。'
    '销售开票沿用原单价格、税费和预收抵扣，已有草稿也占用待开票数量；不再扣库存或登记第二次收款。'
    '新单默认草稿；用户明确要求提交时一次审核就保存并提交，无需先批准草稿再重复批准提交。已有草稿用existing_document复核原单，不另建。'
    '未获批准不执行保存；预检成功后直接调用对应save工具显示系统整单审核卡，不先要求文字确认再点批准。'
    '每次回覆只展示当前结果、具体异常原因及下一步，通常3至6行；全部明细及steps保留在工具结果和审核卡，不在聊天复制整份。'
    '草稿明确尚未入库或记账；已提交必须以工具verified=true和实际单号为据。失败说明失败步骤和原因，rolled_back=true说明本次写入撤回；不得把保存草稿或排队说成入库完成。'
    '采购和开票沿用原生创建与提交权限。权限不足明确需要何种单据权限，禁止让客服提供别人的账号或切换身份。')


def with_finance_guidance(instructions):
    text = instructions or ''
    for marker in (HINT_MARKER, LEGACY_HINT_MARKER):
        text = re.sub(re.escape(marker) + r'[^\r\n]*(?:\r?\n)*', '', text)
    return with_reply_style((text.rstrip() + '\n\n' + HINT).lstrip('\n'))


def tool_definitions():
    result = []
    for module in MODULES:
        path = 'flow.integrations.erpnext.' + module
        module_object = importlib.import_module(path)
        for slug, title, requires_confirmation, description in module_object.TOOLS:
            function = getattr(module_object, slug)
            details = getattr(function, 'description', None) or inspect.getdoc(function) or ''
            description = description + '\n\n' + details
            result.append({'slug': slug, 'title': title, 'requires_confirmation': int(requires_confirmation),
                'description': description, 'import_path': path + '.' + slug})
    return result


def install_finance_tools(enable=False):
    if not frappe.db.exists('DocType', 'Flow Tool'):
        return {'installed': False, 'reason': 'Flow 尚未安装'}
    installed = []
    for definition in tool_definitions():
        slug = definition['slug']
        values = {**definition, 'type': 'Imported', 'code': None, 'summary': definition['title']}
        if enable:
            values['enabled'] = 1
        name = frappe.db.get_value('Flow Tool', {'slug': slug}, 'name')
        if name:
            frappe.db.set_value('Flow Tool', name, values)
        else:
            doc = frappe.get_doc({'doctype': 'Flow Tool', 'enabled': int(enable), **values})
            doc.insert(ignore_permissions=True)
            name = doc.name
        frappe.clear_document_cache('Flow Tool', name)
        installed.append(name)
    agents = []
    if enable:
        for title in ('Flow', '销售助理'):
            name = frappe.db.get_value('Flow Agent', {'title': title}, 'name')
            if not name:
                continue
            agent = frappe.get_doc('Flow Agent', name)
            for name in installed:
                if not any(row.tool == name for row in agent.get('tools') or []):
                    agent.append('tools', {'tool': name})
            agent.instructions = with_finance_guidance(agent.instructions)
            agent.save(ignore_permissions=True, ignore_version=True)
            frappe.clear_document_cache('Flow Agent', agent.name)
            agents.append(agent.name)
    return {'tools': installed, 'enabled': enable, 'agents': agents}
