"""Exact history queries must not silently retarget the active SF label."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_flow_tools import load_flow_tools, Document


class Row(dict):
    __getattr__ = dict.get


@pytest.fixture
def env(monkeypatch):
    f, m = load_flow_tools(monkeypatch)
    def doc(**kw):
        d=Document(**kw);d.as_dict=lambda:dict(d.__dict__);d.has_permission=lambda _p:True;return d
    parent=doc(name='SHIP',shipment_id='CURRENT',awb_number='CURRENT',docstatus=1,status='已发货',
        shipment_amount=99,tracking_status_info='current route',tracking_status='In Progress',pickup_address_name='P',delivery_address_name='D',shipment_delivery_note=[],service_provider='顺丰国际')
    old=doc(name='WB-OLD',shipment='SHIP',waybill='OLD',is_active=0,replacement_status='已替换',
        freight_amount=25,freight_status='已结算',tracking_status_info='old route',label_url='/old.pdf')
    current=doc(name='WB-NEW',shipment='SHIP',waybill='CURRENT',is_active=1,replacement_status='当前',
        freight_amount=99,tracking_status_info='current route',label_url='/current.pdf')
    records={'WB-OLD':old,'WB-NEW':current}
    f.get_doc=lambda dt,name:parent if dt=='Shipment' else records[name]
    def get_all(dt,filters=None,or_filters=None,**kwargs):
        if dt=='SF Waybill':
            return [Row(name=r.name,shipment=r.shipment) for r in records.values()
                if all(r.get(k)==v for k,v in (filters or {}).items())]
        if dt=='Shipment':
            return [Row(name=parent.name)] if any(parent.get(k)==v for k,v in (or_filters or {}).items()) else []
        return []
    f.get_all=get_all
    f.db=SimpleNamespace(exists=lambda *a:True,get_value=lambda *a:None)
    shipping=SimpleNamespace(_sf_waybill=lambda d:d.shipment_id,_has_field=lambda *a:True,
        update_tracking=Mock(return_value={}),track_sf_shipment_readonly=Mock(return_value={'ok':True,'route_count':0}),
        fetch_sf_freight=Mock(return_value={}),CANCELLED_STATUSES=set())
    api=SimpleNamespace(_public_record=lambda r:{**r,'tracking_events':[{'remark':r.get('tracking_status_info')}]},
        list_waybill_records=Mock(return_value={'waybills':[{'name':'WB-OLD','waybill':'OLD','is_active':0},{'name':'WB-NEW','waybill':'CURRENT','is_active':1}]}),
        fetch_waybill_tracking=Mock(return_value={'ok':True,'route_count':0}),
        fetch_waybill_tracking_readonly=Mock(return_value={'ok':True,'route_count':0,'saved_tracking_events':[{'remark':'old route'}]}),
        fetch_waybill_freight=Mock(return_value={'ok':True,'amount':25,'accounting_status':'记账待处理'}))
    m._shipping=lambda:shipping;m._waybill_api=lambda:api
    return SimpleNamespace(f=f,m=m,parent=parent,old=old,current=current,records=records,shipping=shipping,api=api)


def test_old_number_status_uses_only_old_record_fields(env):
    result=env.m.get_sf_shipment_status(waybill='OLD')['shipments'][0]
    assert result['waybill']=='OLD' and result['label_scope']=='历史面单'
    assert result['freight_amount']==25 and result['tracking_info']=='old route'
    assert result['label_url']=='/old.pdf'
    assert env.parent.shipment_amount==99
    assert env.old.permissions==['read'] and env.parent.permissions==['read']


def test_current_and_history_list_are_labelled(env):
    result=env.m.get_sf_shipment_status(shipment='SHIP')['shipments'][0]
    assert result['is_current'] and result['waybill']=='CURRENT'
    assert [r['label_scope'] for r in result['waybills']]==['历史面单','当前面单']


def test_historical_route_uses_exact_record_and_retains_saved_events(env):
    result=env.m.query_sf_tracking(waybill='OLD')['results'][0]
    env.api.fetch_waybill_tracking_readonly.assert_called_once_with('SHIP','WB-OLD')
    env.api.fetch_waybill_tracking.assert_not_called()
    env.shipping.update_tracking.assert_not_called()
    assert result['waybill']=='OLD'
    assert result['tracking']['saved_tracking_events']==[{'remark':'old route'}]
    assert env.parent.tracking_status_info=='current route'


def test_current_route_query_is_read_only(env):
	result = env.m.query_sf_tracking(shipment='SHIP')['results'][0]
	env.api.fetch_waybill_tracking_readonly.assert_called_once_with('SHIP', 'WB-NEW')
	env.api.fetch_waybill_tracking.assert_not_called()
	env.shipping.track_sf_shipment_readonly.assert_not_called()
	env.shipping.update_tracking.assert_not_called()
	assert result['waybill'] == 'CURRENT'


def test_historical_freight_does_not_query_current_parent(env):
    result=env.m.query_sf_freight(waybill='OLD')['results'][0]
    env.api.fetch_waybill_freight.assert_called_once_with('SHIP','WB-OLD')
    env.shipping.fetch_sf_freight.assert_not_called()
    assert result['waybill']=='OLD' and result['amount']==25
    assert result['accounting_status']=='记账待处理'
    assert env.parent.shipment_amount==99


def test_current_freight_uses_current_record(env):
    env.m.query_sf_freight(shipment='SHIP')
    env.api.fetch_waybill_freight.assert_called_once_with('SHIP','WB-NEW')


def test_conflicting_identifiers_reject_without_carrier_call(env):
    with pytest.raises(ValueError,match='不属于'):
        env.m.query_sf_tracking(shipment='OTHER',waybill='OLD')
    env.api.fetch_waybill_tracking.assert_not_called()


def test_mutation_cannot_convert_old_number_to_current(env):
    with pytest.raises(ValueError,match='不是该运单的当前面单'):
        env.m._resolve_shipment_names(shipment='SHIP',waybill='OLD')
    with pytest.raises(ValueError,match='No shipment found'):
        env.m._resolve_shipment_names(waybill='OLD')


def test_duplicate_history_number_is_not_guessed(env):
    env.records['duplicate']=Document(name='duplicate',shipment='SHIP',waybill='OLD')
    with pytest.raises(ValueError,match='多条面单'):
        env.m.get_sf_shipment_status(waybill='OLD')


def test_denied_history_never_falls_back_to_current(env):
    env.old.check_permission=Mock(side_effect=PermissionError('denied'))
    with pytest.raises(PermissionError):
        env.m.query_sf_tracking(waybill='OLD')
    env.api.fetch_waybill_tracking.assert_not_called()


def test_legacy_current_without_history_uses_existing_projection(env):
    env.records.clear()
    result=env.m.get_sf_shipment_status(waybill='CURRENT')['shipments'][0]
    assert result['waybill']=='CURRENT' and result['freight_amount']==99


@pytest.mark.parametrize("action", ["get_sf_shipment_status", "query_sf_tracking", "query_sf_freight"])
@pytest.mark.parametrize("keep_current_record", [True, False])
def test_stale_legacy_number_cannot_retarget_current_record_or_native_parent(env, action, keep_current_record):
    env.parent.awb_number='OLD'
    del env.records['WB-OLD']
    if not keep_current_record:
        env.records.clear()
    with pytest.raises(ValueError,match='不能用当前面单代替'):
        getattr(env.m,action)(waybill='OLD')
    env.api.fetch_waybill_tracking.assert_not_called()
    env.api.fetch_waybill_freight.assert_not_called()
    env.shipping.update_tracking.assert_not_called()
    env.shipping.fetch_sf_freight.assert_not_called()


def test_mutation_by_stale_legacy_number_alone_is_rejected(env):
    env.parent.awb_number='OLD'
    with pytest.raises(ValueError,match='不是该运单的当前面单'):
        env.m._resolve_shipment_names(waybill='OLD')


@pytest.mark.parametrize('explicit',[True,False])
def test_current_shipment_access_survives_missing_history_permission(env,explicit):
    env.current.has_permission=lambda _p:False
    env.current.check_permission=Mock(side_effect=PermissionError('history denied'))
    env.api.list_waybill_records.return_value={'waybills':[],'history_available':False}
    args={'waybill':'CURRENT'} if explicit else {'shipment':'SHIP'}
    result=env.m.get_sf_shipment_status(**args)['shipments'][0]
    assert result['waybill']=='CURRENT' and result['freight_amount']==99
    assert result['status']=='已发货' and result['is_current']
    env.m.query_sf_freight(**args)
    env.shipping.fetch_sf_freight.assert_called_once_with('SHIP')
    env.api.fetch_waybill_freight.assert_not_called()
