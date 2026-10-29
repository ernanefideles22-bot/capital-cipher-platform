"""Virtual venue lifecycle, crash recovery, risk and idempotency regression suite."""
import copy
import hashlib
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.demo.binance import DemoCredentials, DemoError
from app.demo.execution import DemoExecution, identity
from app.demo.venue import FuturesDemoVenue, VenueError


class Venue:
    def __init__(self):
        self.positions=[];self.orders={};self.algos={};self.fills=[];self.calls=[];self.failure=None;self.n=10
    async def validate_account(self):
        return {'authenticated':True,'one_way_mode':True,'single_asset_mode':True,'account_trading_enabled':True,
            'equity_usdt':5000,'available_usdt':5000,'positions':copy.deepcopy(self.positions),
            'open_order_count':0,'open_algo_order_count':sum(a['algoStatus']=='NEW' for a in self.algos.values())}
    async def candles(self,**kwargs):
        return [{'close_time_ms':(int(time.time()*1000)//900000-200+i)*900000+899999,'open':2000,'high':2010,'low':1990,'close':2000,'volume':10} for i in range(200)]
    async def request(self,method,path,params=None,**kwargs):
        params=params or {};self.calls.append((method,path,dict(params)))
        if path.endswith('/premiumIndex'):return {'symbol':'ETHUSDT','markPrice':'2000'}
        if path.endswith('/exchangeInfo'):return {'symbols':[{'symbol':'ETHUSDT','quoteAsset':'USDT','contractType':'PERPETUAL','status':'TRADING','filters':[
            {'filterType':'LOT_SIZE','stepSize':'.001','minQty':'.001','maxQty':'1000'},
            {'filterType':'PRICE_FILTER','tickSize':'.1'},{'filterType':'MIN_NOTIONAL','notional':'20'}]}]}
        if path.endswith('/leverage') or path.endswith('/marginType'):return {}
        if path.endswith('/symbolConfig'):return [{'symbol':'ETHUSDT','leverage':1,'marginType':'ISOLATED'}]
        if path.endswith('/userTrades'):return self.fills
        if path.endswith('/algoOrder'):
            key=params.get('clientAlgoId')
            if method=='GET':
                if key not in self.algos:raise VenueError('NOT_FOUND',venue_code=-2013)
                return self.algos[key]
            if method=='DELETE':
                for a in self.algos.values():
                    if str(a['algoId'])==str(params['algoId']):a['algoStatus']='CANCELED'
                return {'code':'200'}
            if self.failure=='tp' and params['type']=='TAKE_PROFIT_MARKET':raise VenueError('PROTECTION_REJECTED',venue_code=-4120)
            self.n+=1
            self.algos[key]={'algoId':self.n,'clientAlgoId':key,'symbol':params['symbol'],'side':params['side'],
                'positionSide':'BOTH','orderType':params['type'],'algoStatus':'NEW','closePosition':True,'workingType':'MARK_PRICE','triggerPrice':params['triggerPrice']}
            return self.algos[key]
        if path.endswith('/order'):
            if method=='GET':
                row=self.orders.get(params.get('origClientOrderId'))
                if not row:raise VenueError('NOT_FOUND',venue_code=-2013)
                return row
            assert method=='POST'
            self.n+=1; qty=float(params['quantity']);side=params['side'];reduce=params.get('reduceOnly')=='true'
            row={'symbol':'ETHUSDT','side':side,'clientOrderId':params['newClientOrderId'],'orderId':self.n,'status':'FILLED','executedQty':str(qty),'avgPrice':'2000'}
            self.orders[params['newClientOrderId']]=row
            if reduce:
                assert self.positions and side!=self.positions[0]['side']
                self.positions=[]
            else:
                assert not self.positions
                self.positions=[{'symbol':'ETHUSDT','side':side,'quantity':qty,'position_side':'BOTH'}]
            self.fills.append({'orderId':self.n,'realizedPnl':'0','commission':'.02','commissionAsset':'USDT','qty':str(qty)})
            if self.failure=='entry_unknown' and not reduce:raise VenueError('WRITE_UNKNOWN',ambiguous=True)
            if self.failure=='exit_unknown' and reduce:raise VenueError('WRITE_UNKNOWN',ambiguous=True)
            return row
        raise AssertionError((method,path,params))


def proposal(token='proposal-00000001',side='BUY',**overrides):
    result={'id':token,'created_at':datetime.now(timezone.utc).isoformat(),'source':'AGENTS','symbol':'ETHUSDT','timeframe':'15m',
        'candle_closed_at':'2026-10-29T00:00:00+00:00','reference_price':2000,
        'total_agents':300,'agents':[],'primary_agents':[],
        'decision':{'candidate_action':side,'confidence':90},
        'risk':{'approved':True,'risk_status':'REDUCED','approval_id':hashlib.sha256(token.encode()).hexdigest(),'leverage':1,'position_size':50,
            'stop_loss':1970 if side=='BUY' else 2030,'take_profit':2060 if side=='BUY' else 1940,
            'created_at':datetime.now(timezone.utc).isoformat()}}
    result.update(overrides);return result


@pytest.mark.asyncio
@pytest.mark.parametrize('side',['BUY','SELL'])
async def test_protected_entry_exit_and_idempotency(tmp_path,side):
    venue=Venue();engine=DemoExecution(venue,str(tmp_path/'ledger.sqlite'))
    p=proposal(side=side);engine.ledger.proposal(p)
    trade=await engine.execute(p['id']);assert trade['state']=='PROTECTED'
    assert len(venue.positions)==1
    assert len(venue.algos)==2
    for algo in venue.algos.values():
        assert algo['side']!=side and algo['closePosition'] and algo['workingType']=='MARK_PRICE'
    assert (await engine.execute(p['id']))['state']=='PROTECTED'
    assert len([c for c in venue.calls if c[0]=='POST' and c[1].endswith('/order')])==1
    closed=await engine.close_trade(p['id']);assert closed['state']=='CLOSED'
    assert closed['realized_pnl_usdt']==pytest.approx(-.04)
    assert closed['fees_usdt']==pytest.approx(.04)
    assert not venue.positions and all(a['algoStatus']=='CANCELED' for a in venue.algos.values())
    assert len([c for c in venue.calls if c[0]=='POST' and c[1].endswith('/order')])==2
    await engine.close_trade(p['id'])
    assert len(venue.orders)==2
    engine.ledger.close()


@pytest.mark.asyncio
async def test_entry_confirmation_uses_lookup_after_successful_acknowledgement(tmp_path):
    venue=Venue(); original=venue.request
    async def request(method,path,params=None,**kwargs):
        result=await original(method,path,params,**kwargs)
        if method=='POST' and path.endswith('/order') and not (params or {}).get('reduceOnly'):
            return {'orderId':result['orderId']}
        return result
    venue.request=request
    engine=DemoExecution(venue,str(tmp_path/'ledger.sqlite'));p=proposal();engine.ledger.proposal(p)
    assert (await engine.execute(p['id']))['state']=='PROTECTED'
    assert len(venue.orders)==1
    assert any(method=='GET' and path.endswith('/order') for method,path,_ in venue.calls)
    await engine.close_trade(p['id']);engine.ledger.close()


def test_operational_canary_fees_do_not_count_as_strategy_loss_streak(tmp_path):
    engine=DemoExecution(Venue(),str(tmp_path/'ledger.sqlite'))
    for i in range(3):
        engine.ledger.reserve({'id':f'canary-{i}','state':'CLOSED','source':'OPERATIONAL_CANARY','symbol':'ETHUSDT','side':'BUY','realized_pnl_usdt':-.02})
    engine._drawdown_guard({'equity_usdt':4999.94})
    assert not engine.ledger.flag('halted')
    for i in range(3):
        engine.ledger.reserve({'id':f'strategy-{i}','state':'CLOSED','source':'AGENTS','symbol':'ETHUSDT','side':'BUY','realized_pnl_usdt':-.02})
    with pytest.raises(DemoError,match='DRAWDOWN_OR_LOSS_LIMIT'):
        engine._drawdown_guard({'equity_usdt':4999.88})
    assert engine.ledger.flag('halted')
    engine.ledger.close()


@pytest.mark.asyncio
async def test_protection_failure_flattens_once_and_halts(tmp_path):
    venue=Venue();venue.failure='tp';engine=DemoExecution(venue,str(tmp_path/'ledger.sqlite'))
    p=proposal();engine.ledger.proposal(p)
    with pytest.raises(DemoError,match='PROTECTION_REJECTED'):await engine.execute(p['id'])
    assert not venue.positions
    assert engine.ledger.trade(p['id'])['state']=='CLOSED'
    assert engine.ledger.flag('halted')
    assert len(venue.orders)==2
    assert all(a['algoStatus']=='CANCELED' for a in venue.algos.values())
    engine.ledger.close()


@pytest.mark.asyncio
async def test_ambiguous_entry_survives_restart_and_never_resubmits(tmp_path):
    path=str(tmp_path/'ledger.sqlite');venue=Venue();venue.failure='entry_unknown'
    engine=DemoExecution(venue,path);p=proposal();engine.ledger.proposal(p)
    with pytest.raises(DemoError,match='WRITE_UNKNOWN'):await engine.execute(p['id'])
    assert len(venue.positions)==1 and engine.ledger.trade(p['id'])['state']=='UNKNOWN'
    engine.ledger.close();engine=DemoExecution(venue,path)
    await engine.reconcile()
    assert engine.ledger.trade(p['id'])['state']=='CLOSED'
    assert len(venue.orders)==2 and engine.ledger.flag('halted')
    venue.failure=None
    await engine.kill()
    assert not venue.positions and len(venue.orders)==2
    engine.ledger.close()


@pytest.mark.asyncio
async def test_ambiguous_exit_reconciles_flat_without_retry(tmp_path):
    venue=Venue();engine=DemoExecution(venue,str(tmp_path/'ledger.sqlite'));p=proposal();engine.ledger.proposal(p)
    await engine.execute(p['id']);venue.failure='exit_unknown'
    with pytest.raises(DemoError,match='EXIT_UNKNOWN'):await engine.close_trade(p['id'])
    await engine.reconcile()
    assert engine.ledger.trade(p['id'])['state']=='CLOSED' and not venue.positions
    assert len(venue.orders)==2
    engine.ledger.close()


@pytest.mark.asyncio
async def test_missing_stop_after_entry_forces_flat_and_blocks_reentry(tmp_path):
    venue=Venue();engine=DemoExecution(venue,str(tmp_path/'ledger.sqlite'));p=proposal();engine.ledger.proposal(p)
    await engine.execute(p['id']);del venue.algos[identity('sl',p['id'])]
    await engine.reconcile()
    assert not venue.positions and engine.ledger.flag('halted')
    assert len(venue.orders)==2
    engine.ledger.close()


@pytest.mark.asyncio
async def test_conditional_lookup_delay_retries_reads_without_repeating_protection_post(tmp_path):
    venue=Venue();original=venue.request;delayed=set()
    async def request(method,path,params=None,**kwargs):
        key=(params or {}).get('clientAlgoId')
        if method=='GET' and path.endswith('/algoOrder') and key in venue.algos and key not in delayed:
            delayed.add(key)
            raise VenueError('NOT_FOUND',venue_code=-2013)
        return await original(method,path,params,**kwargs)
    venue.request=request
    engine=DemoExecution(venue,str(tmp_path/'ledger.sqlite'));p=proposal();engine.ledger.proposal(p)
    assert (await engine.execute(p['id']))['state']=='PROTECTED'
    assert len([c for c in venue.calls if c[0]=='POST' and c[1].endswith('/algoOrder')])==2
    await engine.close_trade(p['id']);engine.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['expired','unapproved','leverage','notional','bracket','risk_budget','price_moved'])
async def test_invalid_risk_proofs_cannot_reach_entry(tmp_path,kind):
    venue=Venue();engine=DemoExecution(venue,str(tmp_path/'ledger.sqlite'));p=proposal()
    if kind=='expired':p['risk']['created_at']=(datetime.now(timezone.utc)-timedelta(seconds=61)).isoformat()
    if kind=='unapproved':p['risk']['approved']=False
    if kind=='leverage':p['risk']['leverage']=2
    if kind=='notional':p['risk']['position_size']=1
    if kind=='bracket':p['risk']['stop_loss']=2100
    if kind=='risk_budget':p['risk'].update(position_size=200,stop_loss=1900,take_profit=2300)
    if kind=='price_moved':p['reference_price']=1800
    engine.ledger.proposal(p)
    with pytest.raises(DemoError):await engine.execute(p['id'])
    assert not any(c[0]=='POST' and c[1].endswith('/order') for c in venue.calls)
    engine.ledger.close()


@pytest.mark.asyncio
async def test_two_proposals_cannot_open_two_positions(tmp_path):
    venue=Venue();engine=DemoExecution(venue,str(tmp_path/'ledger.sqlite'))
    for i in ('proposal-00001','proposal-00002'):engine.ledger.proposal(proposal(i))
    await engine.execute('proposal-00001')
    with pytest.raises(DemoError):await engine.execute('proposal-00002')
    assert len(venue.positions)==1 and len(venue.orders)==1
    engine.ledger.close()


@pytest.mark.asyncio
async def test_canary_checks_both_sides_and_leaves_automatic_off(tmp_path):
    venue=Venue();n=0
    async def analyze(payload):
        nonlocal n
        n+=1
        return proposal(f'canary-proposal-{n:08}',side=payload['canary_side'],source='OPERATIONAL_CANARY')
    engine=DemoExecution(venue,str(tmp_path/'ledger.sqlite'),analysis_fn=analyze)
    result=await engine.canary()
    assert result['passed'] and result['flat'] and not venue.positions
    assert {r['side'] for r in result['legs']}=={'BUY','SELL'}
    assert engine.ledger.flag('canary_verified') and engine.ledger.flag('automatic') is False
    assert len(venue.orders)==4
    engine.ledger.close()


@pytest.mark.asyncio
async def test_automatic_requires_canary_and_resets_on_restart(tmp_path):
    path=str(tmp_path/'ledger.sqlite');engine=DemoExecution(Venue(),path)
    with pytest.raises(DemoError):await engine.set_automatic(True)
    engine.ledger.flag('canary_verified',True);engine.ledger.flag('canary_revision',engine.revision);await engine.set_automatic(True)
    assert engine.ledger.flag('automatic')
    engine.ledger.close();engine=DemoExecution(Venue(),path)
    assert engine.ledger.flag('automatic') is False
    engine.ledger.close()


@pytest.mark.asyncio
async def test_automatic_analyzes_each_closed_candle_once_and_expires(tmp_path):
    calls=[]
    async def analyze(payload):
        calls.append(payload)
        p=proposal(f'wait-proposal-{len(calls):08}')
        p['candle_closed_at']=datetime.fromtimestamp(payload['candles'][-1]['close_time_ms']/1000,timezone.utc).isoformat()
        p.update(decision={'candidate_action':'WAIT','confidence':40,'reason':'Insufficient confidence'},risk=None)
        return p
    venue=Venue();engine=DemoExecution(venue,str(tmp_path/'ledger.sqlite'),analysis_fn=analyze)
    engine.ledger.flag('canary_verified',True);engine.ledger.flag('canary_revision',engine.revision)
    await engine.set_automatic(True)
    baseline=engine.ledger.flag('observation')
    for _ in range(3):await engine.cycle()
    assert len(calls)==1 and not venue.orders
    report=engine.observation()
    assert report['analysis_count']==report['wait_count']==1
    assert report['net_realized_pnl_usdt']==0 and report['settled_count']==0
    assert report['baseline_equity_usdt']==5000
    await engine.set_automatic(False);await engine.set_automatic(True)
    assert engine.ledger.flag('observation')==baseline
    campaign=engine.ledger.flag('observation');campaign['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    engine.ledger.flag('observation',campaign)
    await engine.cycle()
    assert not engine.ledger.flag('automatic') and len(calls)==1
    assert any(e['kind']=='OBSERVATION_EXPIRED' for e in engine.ledger.events())
    engine.ledger.close()


def test_observation_uses_full_campaign_history_and_excludes_canaries(tmp_path):
    engine=DemoExecution(Venue(),str(tmp_path/'ledger.sqlite'))
    engine.ledger.flag('observation',{'id':'campaign-1','started_at':utc_stamp(),'expires_at':utc_stamp(),'baseline_equity_usdt':5000})
    for i in range(105):
        engine.ledger.reserve({'id':f'trade-{i}','symbol':'ETHUSDT','side':'BUY','state':'CLOSED','source':'AGENTS',
            'observation_id':'campaign-1','realized_pnl_usdt':-.1,'fees_usdt':.04})
    engine.ledger.reserve({'id':'canary-trade','symbol':'ETHUSDT','side':'BUY','state':'CLOSED','source':'OPERATIONAL_CANARY',
        'observation_id':'campaign-1','realized_pnl_usdt':-50,'fees_usdt':50})
    engine.ledger.reserve({'id':'other-campaign','symbol':'ETHUSDT','side':'BUY','state':'CLOSED','source':'AGENTS',
        'observation_id':'campaign-0','realized_pnl_usdt':100})
    report=engine.observation()
    assert len(engine.ledger.trades())==100
    assert report['settled_count']==report['losses']==105
    assert report['net_realized_pnl_usdt']==pytest.approx(-10.5)
    assert report['fees_usdt']==pytest.approx(4.2)
    engine.ledger.close()


def utc_stamp():
    return datetime.now(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_central_control_guard_prevents_connector_entry_and_automation(tmp_path):
    async def blocked():raise DemoError('DEMO_CENTRAL_CONTROL_BLOCKED')
    venue=Venue();engine=DemoExecution(venue,str(tmp_path/'ledger.sqlite'),entry_guard=blocked)
    p=proposal();engine.ledger.proposal(p)
    engine.ledger.flag('canary_verified',True);engine.ledger.flag('canary_revision',engine.revision)
    with pytest.raises(DemoError,match='CENTRAL_CONTROL_BLOCKED'):await engine.execute(p['id'])
    with pytest.raises(DemoError,match='CENTRAL_CONTROL_BLOCKED'):await engine.set_automatic(True)
    assert not venue.orders and not engine.ledger.flag('automatic')
    engine.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['timeout','redirect','rejection'])
async def test_transport_write_errors_classified_without_secret_leak(failure):
    secret='never-log-this-secret-123456789'
    async def handler(request):
        assert request.url.host=='demo-fapi.binance.com'
        if request.url.path.endswith('/time'):return httpx.Response(200,json={'serverTime':1800000000000})
        assert request.method=='POST' and request.headers['X-MBX-APIKEY']=='demo-key-12345'
        if failure=='timeout':raise httpx.ReadTimeout(secret,request=request)
        if failure=='redirect':return httpx.Response(302,headers={'Location':'https://fapi.binance.com/fapi/v1/order'},json={})
        return httpx.Response(400,json={'code':-4120,'msg':secret})
    venue=FuturesDemoVenue(DemoCredentials('demo-key-12345',secret),transport=httpx.MockTransport(handler),clock_ms=lambda:1800000000000)
    await venue.sync_clock()
    with pytest.raises(VenueError) as caught:await venue.request('POST','/fapi/v1/order',{'symbol':'ETHUSDT'})
    assert caught.value.ambiguous is (failure!='rejection')
    assert secret not in str(caught.value)
    await venue.aclose()
