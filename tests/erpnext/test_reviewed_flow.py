"""Transaction and approval boundary tests, including actual simulated side effects."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import pytest


class Callbacks:
    def __init__(self): self._functions = []
    def reset(self): self._functions.clear()
    def add(self, f): self._functions.append(f)


@pytest.fixture
def env(monkeypatch):
    f = ModuleType('frappe')
    f.local = SimpleNamespace(site='site', _realtime_log=['earlier'], document_cache={})
    f.session = SimpleNamespace(user='user-a')
    f.PermissionError = PermissionError
    f.DuplicateEntryError = KeyError
    f.scrub = lambda x: x.lower().replace(' ', '_')
    data, checkpoints, cache = {'docs': {}, 'stock': 10}, {}, {}
    f.cache = SimpleNamespace(get_value=cache.get, set_value=lambda k,v,**kw:cache.__setitem__(k,deepcopy(v)))
    def rollback(save_point=None):
        data.clear();data.update(deepcopy(checkpoints[save_point]))
    f.db = SimpleNamespace(savepoint=lambda k:checkpoints.__setitem__(k,deepcopy(data)), rollback=rollback,
        exists=lambda dt,n:(dt,n) in data['docs'])
    for key in ('before_commit', 'after_commit', 'before_rollback', 'after_rollback'):
        setattr(f.db,key,Callbacks())
    f.db.after_commit.add('earlier-job')
    denied = set()
    class Doc:
        def __init__(self, values):
            self.__dict__.update(values);self.flags=SimpleNamespace();self.docstatus=values.get('docstatus',0)
            self.name=values.get('name');self._new=self.name is None
        def is_new(self): return self._new
        def check_permission(self, p):
            if (self.doctype,p) in denied:raise PermissionError('denied '+p)
        def insert(self, ignore_permissions=False):
            if self.doctype!='Integration Request':assert not ignore_permissions
            self.name=getattr(self.flags,'_name',None) or 'DOC-1'
            if (self.doctype,self.name) in data['docs']:raise KeyError('duplicate')
            self._new=False;data['docs'][(self.doctype,self.name)]=deepcopy(self);return self
        def save(self, ignore_permissions=False):
            if self.doctype!='Integration Request':assert not ignore_permissions
            data['docs'][(self.doctype,self.name)]=deepcopy(self);return self
        def submit(self):
            self.docstatus=1;self.save();data['stock']+=5
            f.db.after_commit.add('submitted-job');f.local._realtime_log.append('submitted')
        def update(self, values):self.__dict__.update(values)
    def get_doc(dt, name=None, **kw):
        return Doc(dt) if isinstance(dt,dict) else deepcopy(data['docs'][(dt,name)])
    f.get_doc=get_doc
    util=ModuleType('frappe.utils');util.now_datetime=lambda:datetime(2026,9,18,10);util.get_datetime=datetime.fromisoformat;util.strip_html=str
    common=ModuleType('flow.integrations.erpnext.sales_order_flow')
    common._actor=lambda:f.session.user;common._scope=lambda:getattr(f,'scope','chat-a')
    common._json=lambda x:json.dumps(x,sort_keys=True,default=str)
    common._hash=lambda x:hashlib.sha256(common._json(x).encode()).hexdigest()
    common._without_price_maintenance=nullcontext
    for name,module in {'frappe':f,'frappe.utils':util,common.__name__:common}.items():monkeypatch.setitem(sys.modules,name,module)
    path=Path(__file__).resolve().parents[2]/'flow/integrations/erpnext/reviewed_flow.py'
    spec=importlib.util.spec_from_file_location('flow.integrations.erpnext.reviewed_flow',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    e=SimpleNamespace(f=f,m=module,data=data,cache=cache,denied=denied,amount=600,Doc=Doc)
    e.build=lambda request,for_update=False:Doc({'doctype':'Purchase Receipt','amount':e.amount})
    e.summary=lambda doc,request:{'title':'入库','action':'提交' if request.get('submit') else '草稿','totals':{'金额':doc.amount},'fields':{},'items':[]}
    e.preview=lambda submit=True:module.preview('purchase_receipt',{'submit':submit},e.build,e.summary)
    e.execute=lambda token,verify=None:module.execute('purchase_receipt',token,e.build,e.summary,verify=verify)
    return e


def test_preview_rolls_back_native_side_effects_and_callbacks(env):
    original=env.build
    def build(*a,**kw):
        env.data['stock']=100;env.f.db.after_commit.add('preview-job');env.f.local._realtime_log.append('preview')
        return original(*a,**kw)
    env.build=build
    result=env.preview()
    assert result['status']=='preview' and env.data['stock']==10 and not env.data['docs']
    assert env.f.db.after_commit._functions==['earlier-job'] and env.f.local._realtime_log==['earlier']


def test_submit_verification_failure_rolls_back_doc_stock_journal_and_callbacks(env):
    token=env.preview()['preview_token']
    def fail(doc,request):raise ValueError('missing GL')
    result=env.execute(token,verify=fail)
    assert result['status']=='error' and result['failed_step']=='verify' and result['rolled_back']
    assert 'missing GL' in result['reason'] and not env.data['docs'] and env.data['stock']==10
    assert env.f.db.after_commit._functions==['earlier-job'] and env.f.local._realtime_log==['earlier']


def test_repeated_approved_token_posts_only_once_even_after_cache_expiry(env):
    token=env.preview()['preview_token']
    first=env.execute(token);env.cache.clear();second=env.execute(token)
    assert first['status']=='submitted' and second['status']=='existing'
    assert first['document']==second['document'] and env.data['stock']==15
    assert len(env.data['docs'])==2


def test_changed_amount_cannot_reuse_approval(env):
    token=env.preview()['preview_token'];env.amount=601
    result=env.execute(token)
    assert result['status']=='needs_input' and '变化' in result['reason'] and not env.data['docs']


@pytest.mark.parametrize('change',['user','scope','site','expiry'])
def test_foreign_or_expired_plan_is_rejected(env,change):
    token=env.preview()['preview_token']
    if change=='user':env.f.session.user='user-b'
    if change=='scope':env.f.scope='chat-b'
    if change=='site':env.f.local.site='other-site'
    if change=='expiry':next(iter(env.cache.values()))['expires_at']='2020-01-01'
    assert env.execute(token)['verified'] is False and not env.data['docs']


def test_permission_removed_after_preview_blocks_write(env):
    token=env.preview()['preview_token'];env.denied.add(('Purchase Receipt','submit'))
    assert env.execute(token)['status']=='error' and not env.data['docs']


def test_draft_never_posts_and_card_uses_stored_amount(env):
    plan=env.preview(False);card=env.m.confirmation('purchase_receipt',plan['preview_token'])
    assert '600' in card and '草稿' in card
    assert env.execute(plan['preview_token'])['status']=='draft' and env.data['stock']==10


def test_existing_manual_change_is_reported_without_new_write(env):
    token=env.preview()['preview_token'];env.execute(token)
    env.data['docs'][('Purchase Receipt','DOC-1')].amount=700
    result=env.execute(token)
    assert result['status']=='existing_changed' and not result['verified'] and result['summary']['totals']['金额']==700
    assert env.data['stock']==15


def test_save_changes_unapproved_amount_is_rolled_back(env):
    token=env.preview()['preview_token'];original=env.Doc.insert
    def insert(self,**kw):
        if self.doctype=='Purchase Receipt':self.amount=999
        return original(self,**kw)
    env.Doc.insert=insert
    result=env.execute(token)
    assert result['rolled_back'] and not env.data['docs']
