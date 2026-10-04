"""Native invoice mapping, reviewed once, without a second stock issue."""
from collections import defaultdict
from datetime import date
import math

import frappe
from frappe.utils import nowdate, flt
from flow.lib.tool import tool

from .reviewed_flow import InputError, preview, execute, confirmation, failure
from .sales_order_flow import _without_price_maintenance

KIND = 'sales_invoice'
SOURCE_TYPES = {'Sales Order': ('so_detail', 'sales_order'), 'Delivery Note': ('dn_detail', 'delivery_note')}
FIELDS = {'source_type', 'source_document', 'source_revision', 'items', 'all_rows', 'posting_date',
          'due_date', 'submit', 'existing_document', 'existing_revision', 'new_request'}


def _read(dt, name, lock=False):
    doc = frappe.get_doc(dt, name, for_update=lock)
    doc.check_permission('read')
    return doc


def _number(value):
    if isinstance(value, bool):
        raise InputError('开票数量须为实际正数。', ['items.qty'])
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise InputError('开票数量须为实际正数。', ['items.qty']) from None
    if not math.isfinite(value) or value <= 0:
        raise InputError('开票数量须为有限正数。', ['items.qty'])
    return value


def _request(request):
    if not isinstance(request, dict) or set(request) - FIELDS:
        raise InputError('开票参数不支持覆盖原单价格、会计科目或直接扣库存。', ['request'])
    r = dict(request)
    for flag in ('submit', 'new_request', 'all_rows'):
        if flag in r and type(r[flag]) is not bool:
            raise InputError(flag + ' 必须为 true 或 false。', [flag])
    r.setdefault('submit', False)
    if r.get('existing_document'):
        if set(r) - {'existing_document', 'existing_revision', 'submit', 'new_request'}:
            raise InputError('审核已有发票草稿时不能同时覆盖原单字段。')
        return r
    if r.get('source_type') not in SOURCE_TYPES or not r.get('source_document'):
        raise InputError('请选择原销售订单或出库单。', ['source_type', 'source_document'])
    if bool(r.get('items')) == bool(r.get('all_rows')):
        raise InputError('请明确选择本次开票行及数量，或明确全部剩余开票。', ['items'])
    if r.get('items') and (not isinstance(r['items'], list) or len(r['items']) > 100):
        raise InputError('一次最多处理100行开票明细。')
    r.setdefault('posting_date', nowdate())
    for key in ('posting_date', 'due_date'):
        if r.get(key):
            try:
                date.fromisoformat(r[key])
            except (ValueError, TypeError):
                raise InputError(key + ' 须为 YYYY-MM-DD。', [key]) from None
    return r


def _source(dt, name, lock=False):
    if dt not in SOURCE_TYPES:
        raise InputError('仅支持普通销售订单或出库单开票。')
    doc = _read(dt, name, lock)
    if doc.docstatus != 1 or doc.get('is_return') or doc.get('status') in ('Closed', 'Cancelled', 'On Hold'):
        raise InputError('开票来源须已提交且有效，退货、关闭和暂停单据需在原生界面处理。')
    if doc.get('has_unit_price_items') or doc.get('is_subcontracted') or doc.get('is_internal_customer'):
        raise InputError('单位价格、内部交易或分包来源请使用原生开票页面核对。')
    if dt == 'Delivery Note' and frappe.db.exists(dt, {'return_against': name, 'docstatus': 1}):
        raise InputError('该出库单存在退货，请在原生页面核对净开票数量。')
    return doc


def _invoiced(field, row_ids, excluded=None):
    if field not in ('so_detail', 'dn_detail'):
        raise ValueError(field)
    if not row_ids:
        return {}, {}
    rows = frappe.db.sql(f'''select i.`{field}` as source_row,i.stock_qty,i.qty,i.conversion_factor,
        p.name,p.docstatus,p.is_return,p.update_stock from `tabSales Invoice Item` i
        inner join `tabSales Invoice` p on p.name=i.parent
        where p.docstatus<2 and i.`{field}` in %(ids)s and p.name<>%(excluded)s''',
        {'ids': list(row_ids), 'excluded': excluded or ''}, as_dict=True)
    used, draft = defaultdict(float), defaultdict(float)
    for row in rows:
        if row.is_return or flt(row.qty) < 0:
            raise InputError('来源存在退货／红字发票，请先核对原生单据，不能简单累计正负数量。')
        qty = flt(row.stock_qty)
        if qty <= 0:
            raise InputError('关联发票缺少有效库存单位数量，须先核对原单。')
        used[row.source_row] += qty
        if row.docstatus == 0:
            draft[row.source_row] += qty
    return used, draft


def _available(source, excluded=None, lock=False):
    field, _ = SOURCE_TYPES[source.doctype]
    used, draft = _invoiced(field, [r.name for r in source.items], excluded)
    so_docs = {}
    if source.doctype == 'Delivery Note':
        for name in sorted({r.against_sales_order for r in source.items if r.against_sales_order}):
            so_docs[name] = _source('Sales Order', name, lock)
    order_rows = {r.name: r for d in so_docs.values() for r in d.items}
    so_used, _ = _invoiced('so_detail', list(order_rows), excluded)
    rows = []
    for row in source.items:
        factor = _number(row.conversion_factor)
        available = max(0, flt(row.stock_qty) - used.get(row.name, 0))
        order_remaining = None
        if source.doctype == 'Delivery Note' and row.get('against_sales_order'):
            order = so_docs[row.against_sales_order]
            original = order_rows.get(row.so_detail)
            if not original or original.item_code != row.item_code or order.customer != source.customer or order.company != source.company:
                raise InputError('出库行与销售订单的客户、公司或物料关联不一致。')
            order_remaining = max(0, flt(original.stock_qty) - so_used.get(original.name, 0))
            available = min(available, order_remaining)
        rows.append({'source_row': row.name, 'item_code': row.item_code, 'item_name': row.item_name,
            'uom': row.uom, 'conversion_factor': factor, 'stock_uom': row.stock_uom,
            'available_qty': available / factor, 'available_stock_qty': available,
            'draft_reserved_qty': draft.get(row.name, 0) / factor, 'rate': flt(row.rate),
            'so_detail': row.get('so_detail') if source.doctype == 'Delivery Note' else row.name,
            'order_remaining': order_remaining})
    return rows


def _selection(rows, requested, all_rows=False):
    available = {r['source_row']: r for r in rows}
    if all_rows:
        requested = [{'source_row': r['source_row'], 'qty': r['available_qty']} for r in rows if r['available_qty'] > 1e-6]
    selected, per_order = {}, defaultdict(float)
    for choice in requested or []:
        if not isinstance(choice, dict) or set(choice) - {'source_row', 'qty'} or choice.get('source_row') not in available:
            raise InputError('开票明细必须使用工具返回的真实来源行编号。', ['items'])
        name = choice['source_row']
        if name in selected:
            raise InputError('同一来源行不能重复选择。')
        row, qty = available[name], _number(choice.get('qty'))
        if qty > row['available_qty'] + 1e-6:
            raise InputError(f"{row['item_code']}本次最多可开票 {row['available_qty']:g} {row['uom']}，已扣除已有发票和草稿。")
        if row['so_detail'] and row['order_remaining'] is not None:
            per_order[row['so_detail']] += qty * row['conversion_factor']
            if per_order[row['so_detail']] > row['order_remaining'] + 1e-6:
                raise InputError('同一订单商品分多条出库行，本次合计超过订单剩余可开票数量。')
        selected[name] = qty
    if not selected:
        raise InputError('没有剩余可开票数量，或未明确选择本次开票内容。')
    return selected


def _validate(doc):
    doc._action = 'save'
    doc.flags.ignore_permissions = False  # native SO mapper sets this flag internally
    if doc.docstatus != 0 or doc.is_return or doc.update_stock or doc.is_pos or doc.is_opening == 'Yes' or doc.get('is_debit_note'):
        raise InputError('仅处理普通销售发票草稿，不能借此工具退货、POS收款或直接扣库存。')
    doc.check_permission('create' if doc.is_new() else 'write')
    doc.run_method('before_validate')
    doc.run_method('validate')
    original_name = doc.name
    if not original_name:
        doc.name = 'new-flow-sales-invoice-preview'
        doc.set_parent_in_children()
    try:
        doc._validate()
    finally:
        doc.name = original_name
        doc.set_parent_in_children()
    for row in doc.items:
        _number(row.qty)
        if not math.isfinite(flt(row.rate)) or flt(row.rate) < 0 or flt(row.net_amount) < 0:
            raise InputError('开票价格或净额无效，请先核对原单。')


def _build(request, for_update=False):
    r = _request(request)
    if r.get('existing_document'):
        doc = _read('Sales Invoice', r['existing_document'], for_update)
        if r.get('existing_revision') and str(doc.modified) != r['existing_revision']:
            raise InputError('原发票草稿已被修改，请重新预检。')
        groups = defaultdict(list)
        for row in doc.items:
            dt, name, detail = ('Delivery Note', row.delivery_note, row.dn_detail) if row.delivery_note else ('Sales Order', row.sales_order, row.so_detail)
            if not name or not detail:
                raise InputError('该发票存在无来源行，请在原生界面审核。')
            groups[(dt, name)].append({'source_row': detail, 'qty': row.qty})
        if len(groups) != 1:
            raise InputError('合并多个来源的发票请在原生界面审核。')
        (dt, name), selected = next(iter(groups.items()))
        source = _source(dt, name, for_update)
        if doc.customer != source.customer or doc.company != source.company:
            raise InputError('已有发票与来源客户或公司不一致。')
        available = _available(source, excluded=doc.name, lock=for_update)
        source_units = {r['source_row']: r for r in available}
        for row in doc.items:
            detail = row.dn_detail if dt == 'Delivery Note' else row.so_detail
            original = source_units.get(detail)
            if not original or row.uom != original['uom'] or abs(flt(row.conversion_factor) - original['conversion_factor']) > 1e-6:
                raise InputError('已有发票改变了来源计量单位，请在原生页面核对换算后开票。')
        _selection(available, selected)
    else:
        source = _source(r['source_type'], r['source_document'], for_update)
        if r.get('source_revision') and str(source.modified) != r['source_revision']:
            raise InputError('来源单据已变化，请重新查看剩余明细并审核。')
        selected = _selection(_available(source, lock=for_update), r.get('items'), r.get('all_rows'))
        if source.doctype == 'Sales Order':
            from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
        else:
            from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice
        doc = make_sales_invoice(source.name, args={'filtered_children': list(selected)})
        doc.flags.ignore_permissions = False
        field, _ = SOURCE_TYPES[source.doctype]
        if {row.get(field) for row in doc.items} != set(selected):
            raise InputError('原生可开票明细与所选内容不一致，请核对原单。')
        for row in doc.items:
            row.qty = selected[row.get(field)]
        doc.update({'posting_date': r['posting_date'], 'update_stock': 0, 'is_pos': 0})
        if r.get('due_date'):
            doc.due_date = r['due_date']
        doc.run_method('set_missing_values')
        doc.run_method('calculate_taxes_and_totals')
        if doc.get('allocate_advances_automatically'):
            doc.set_advances()
    _validate(doc)
    return doc


def _summary(doc, request):
    return {'title': '销售开票审核', 'action': '提交销售发票' if request.get('submit') else '保存销售发票草稿',
        'fields': {'已有草稿': request.get('existing_document') or '', '客户': doc.customer, '公司': doc.company,
            '币种': doc.currency, '本位币汇率': flt(doc.conversion_rate), '开票日期': str(doc.posting_date),
            '到期日期': str(doc.due_date), '应收科目': doc.debit_to, '收款账户币种': doc.party_account_currency,
            '账单地址': doc.get('address_display') or '', '收货地址': doc.get('shipping_address') or '',
            '税费': [{'类型': r.charge_type, '科目': r.account_head, '税率': flt(r.rate), '金额': flt(r.tax_amount)} for r in doc.taxes],
            '预收款抵扣': [{'类型': r.reference_type, '原单': r.reference_name, '金额': flt(r.allocated_amount),
                '原收款行': r.reference_row or '', '参考汇率': flt(r.ref_exchange_rate), '汇兑差额': flt(r.exchange_gain_loss),
                '差额入账日期': str(r.difference_posting_date or '')} for r in doc.advances]},
        'items': [{'来源订单': r.sales_order or '', '来源出库单': r.delivery_note or '', '来源行': r.dn_detail or r.so_detail,
            '物料': r.item_code, '数量': flt(r.qty), '单位': r.uom, '单价': flt(r.rate), '净额': flt(r.net_amount),
            '收入科目': r.income_account, '成本中心': r.cost_center, '项目': r.get('project') or ''} for r in doc.items],
        'totals': {'净额': flt(doc.net_total), '税额': flt(doc.total_taxes_and_charges), '总额': flt(doc.grand_total),
            '舍入总额': flt(doc.rounded_total), '禁用舍入': bool(doc.disable_rounded_total), '预收抵扣': flt(doc.total_advance)},
        'effects': ['提交会按原生规则确认收入、税费、应收及已选预收款抵扣；不另行扣库存，不创建新的收款。'] if request.get('submit') else ['仅保存草稿，尚未确认应收或收入。'],
        'warnings': ['免费贴纸可为零售价；库存成本仍以真实出库流水为准。'] if any(flt(r.rate) == 0 for r in doc.items) else []}


def _verify(doc, request):
    if not request.get('submit'):
        return
    if frappe.db.exists('Stock Ledger Entry', {'voucher_type': 'Sales Invoice', 'voucher_no': doc.name, 'is_cancelled': 0}):
        raise InputError('普通开票出现了额外库存流水，本次提交须撤回。')
    rows = frappe.get_all('GL Entry', filters={'voucher_type': 'Sales Invoice', 'voucher_no': doc.name, 'is_cancelled': 0}, fields=['company', 'debit', 'credit'])
    if not rows and doc.get_gl_entries():
        raise InputError('销售发票缺少应有会计流水。')
    if any(r.company != doc.company for r in rows) or abs(sum(flt(r.debit) - flt(r.credit) for r in rows)) > 0.01:
        raise InputError('销售发票会计流水公司或借贷金额不一致。')


def get_sales_invoice_options(source_type: str, source_document: str):
    """### 参数与默认值

    - `source_type` 为 `Sales Order` 或 `Delivery Note`；`source_document` 为真实来源单号。

    ### 返回与下一步

    - 返回已扣除现有草稿占用的实际剩余数量；按明确选择或全部剩余要求调用 `preview_sales_invoice`。

    ### 限制

    - 不保存、不提交发票。
    """
    try:
        source = _source(source_type, source_document)
        return {'status': 'needs_selection', 'reason': '请指定本次行及数量，或明确全部剩余开票。',
            'source_type': source_type, 'source_document': source.name, 'source_revision': str(source.modified),
            'customer': source.customer, 'company': source.company, 'currency': source.currency,
            'items': _available(source)}
    except Exception as exc:
        return failure(exc)


def preview_sales_invoice(request: dict):
    """### 参数与默认值

    - `request` 使用 `source_type`（`Sales Order/Delivery Note`）、`source_document` 和 `items[{source_row,qty}]`；明确全部剩余时可传 `all_rows=true`。
    - 默认 `submit=false`，明确要求提交才传 `true`；可选 `posting_date`、`due_date`。
    - 已有草稿仅传 `existing_document` 和 `submit`，复核原单。

    ### 返回与下一步

    - 核对原单价格、税费、预收抵扣与保存或提交动作，返回 `preview_token`，随后调用 `save_sales_invoice` 整单批准一次。

    ### 限制

    - 不通过本工具退货、直接扣库存或额外收款。
    """
    try:
        r = _request(request)
        if r.get('existing_document'):
            r['existing_revision'] = str(_read('Sales Invoice', r['existing_document']).modified)
        else:
            r['source_revision'] = str(_source(r['source_type'], r['source_document']).modified)
        return preview(KIND, r, _build, _summary)
    except Exception as exc:
        return failure(exc)


@tool(requires_confirmation=True, confirm_prompt=lambda args: confirmation(KIND, args.get('preview_token')))
def save_sales_invoice(preview_token: str):
    """### 参数与默认值

    - 使用 `preview_sales_invoice` 返回的真实 `preview_token`；动作沿用已审核方案。

    ### 返回与下一步

    - 整单批准一次后执行；失败撤回本次写入，重复批准返回原单。
    - 以 `verified=true` 和真实单号核实保存或提交结果。

    ### 限制

    - 不重复扣库存、不登记第二次收款。
    """
    return execute(KIND, preview_token, _build, _summary, verify=_verify)


TOOLS = [
    ('get_sales_invoice_options', '查询待开票明细', False, '只读查询已提交销售订单或出库单的剩余可开票明细。'),
    ('preview_sales_invoice', '预检销售开票', False, '按原生规则预检销售开票并生成审核方案。'),
    ('save_sales_invoice', '保存或提交销售发票', True, '整体批准后保存或提交已审核的原生销售发票。'),
]
HINT = '销售开票先查询剩余明细，使用已知明确选择或全部剩余要求；原生价格税费与预收抵扣完整展示。默认草稿，明确要求提交才submit=true。已收款不等于已开票，开票不等于再收款；不重复扣库存。不支持的退货、合并或特殊业务说明原因并提供原生入口。'
