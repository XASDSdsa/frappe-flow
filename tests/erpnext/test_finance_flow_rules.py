"""Financial source selection and isolation boundaries without a live database."""
import ast
import importlib.util
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
import math
import sys
import pytest

ROOT=Path(__file__).resolve().parents[2]/'flow/integrations/erpnext'
class InputError(Exception):
    def __init__(self,msg,fields=None):super().__init__(msg);self.fields=fields


def functions(file,names,**globals_):
    tree=ast.parse((ROOT/file).read_text())
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    for node in nodes:node.decorator_list=[]
    namespace={'InputError':InputError,'math':math,'defaultdict':defaultdict,**globals_}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),file,'exec'),namespace)
    return SimpleNamespace(**{k:namespace[k] for k in names})


def rows():
    return [{'source_row':'A','item_code':'STICKER','uom':'Nos','conversion_factor':1,'available_qty':100,
             'so_detail':'SO-A','order_remaining':120},
            {'source_row':'B','item_code':'STICKER','uom':'Nos','conversion_factor':1,'available_qty':100,
             'so_detail':'SO-A','order_remaining':120}]


@pytest.mark.parametrize('value',[True,0,-1,float('inf'),float('nan'),'bad',None])
def test_invoice_invalid_quantities(value):
    m=functions('sales_invoice_flow.py',{'_number'})
    with pytest.raises(InputError):m._number(value)


@pytest.mark.parametrize('requested',[[{'source_row':'A','qty':101}], [{'source_row':'X','qty':1}],
    [{'source_row':'A','qty':1},{'source_row':'A','qty':1}],
    [{'source_row':'A','qty':70},{'source_row':'B','qty':60}]])
def test_invoice_selection_prevents_wrong_rows_and_cross_delivery_overbilling(requested):
    m=functions('sales_invoice_flow.py',{'_number','_selection'})
    with pytest.raises(InputError):m._selection(rows(),requested)


def test_invoice_zero_price_sticker_quantity_is_not_lost():
    m=functions('sales_invoice_flow.py',{'_number','_selection'})
    assert m._selection(rows(),[{'source_row':'A','qty':100}])=={'A':100}


def test_invoice_reservations_count_both_posted_and_draft_stock_units():
    data=[SimpleNamespace(source_row='A',is_return=0,qty=10,stock_qty=100,docstatus=1),
          SimpleNamespace(source_row='A',is_return=0,qty=3,stock_qty=30,docstatus=0)]
    f=SimpleNamespace(db=SimpleNamespace(sql=lambda *a,**kw:data))
    m=functions('sales_invoice_flow.py',{'_invoiced'},frappe=f,flt=lambda x:float(x or 0))
    used,drafts=m._invoiced('so_detail',['A'])
    assert used['A']==130 and drafts['A']==30
    data[0].is_return=1
    with pytest.raises(InputError):m._invoiced('so_detail',['A'])


def test_finance_guidance_retires_old_block_and_preserves_procurement_and_invoicing(monkeypatch):
    monkeypatch.setitem(sys.modules, 'frappe', SimpleNamespace())
    source = ROOT / 'finance_flow_install.py'
    spec = importlib.util.spec_from_file_location('finance_install_under_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = ('保留独立业务规则。\n' + module.LEGACY_HINT_MARKER + '旧利润规则 query_sticker_order_profit。\n\n'
                + module.HINT_MARKER + '旧采购规则。\n保留结尾规则。')

    updated = module.with_finance_guidance(original)

    assert '保留独立业务规则。' in updated and '保留结尾规则。' in updated
    assert module.LEGACY_HINT_MARKER not in updated
    assert 'query_sticker_order_profit' not in updated
    assert '旧采购规则' not in updated
    assert 'preview_purchase_receipt→save_purchase_receipt' in updated
    assert 'preview_sales_invoice→save_sales_invoice' in updated
    assert updated.count(module.HINT_MARKER) == 1
    assert module.with_finance_guidance(updated) == updated
