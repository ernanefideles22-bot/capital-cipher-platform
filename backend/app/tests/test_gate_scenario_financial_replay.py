import copy
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D

import pytest

from app.strategy import gate_scenario_financial_replay as f
from app.strategy import gate_scenario_observer as o
from app.strategy.gate_s2_replay import _exit_trigger
from types import SimpleNamespace


def plan(side='LONG',slip=6):
    return {'side':side,'target':130 if side=='LONG' else 70,'policy':{'slippage_bps_side':slip,
        'fee_bps_side':5,'minimum_net_reward_risk':2}}


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_integer_lot_sizing_respects_cost_adjusted_risk_and_notional_caps(side):
    p=plan(side);stop=95 if side=='LONG' else 105
    q=f.entry_quote(p,stop,100,1000)
    assert q is not None
    cost=o.economics(p,100,stop)['net_risk_per_base_unit']
    assert q['quantity']*cost<=2.5+1e-10
    assert q['quantity']*q['entry']<=1000
    assert D(str(q['quantity']))%D('.0001')==0


@pytest.mark.parametrize('direction',[1,-1])
@pytest.mark.parametrize('rate',[.0001,-.0001])
def test_funding_cash_sign_bounds_and_boundary_ambiguity(direction,rate):
    mark={'o':'100','l':'90','h':'110'};previous={'l':'80','h':'120'}
    point,low,high=f.signed_funding(2,direction,rate,mark,previous)
    assert point==pytest.approx(-direction*2*rate*100)
    assert low<=point<=high
    b=f.signed_funding(2,direction,rate,mark,previous,boundary=True)
    assert b[0]==0 and b[1]<=0<=b[2]
    assert b[1]<=point<=b[2]


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_closed_trade_reconciles_independent_decimal_four_cash_components(side):
    p=plan(side);stop=95 if side=='LONG' else 105
    position=f.entry_quote(p,stop,100,1000)
    position.update(entry_t=100,candidate_at=42,scenario_id='fixed',funding_events=[])
    position['funding']=-.012;position['funding_low']=-.014;position['funding_high']=-.01
    raw=110 if side=='LONG' else 90
    r=f.close_trade(position,raw,'TARGET',400)
    q=D(str(position['quantity']));entry=D(str(position['entry']))
    exit_price=D(raw)*(1-D(position['direction'])*D('.0006'))
    gross=q*D(position['direction'])*(exit_price-entry)
    fees=(entry+exit_price)*q*D('.0005')
    assert r['net']==pytest.approx(float(gross-fees-D('.012')))
    assert r['net_lower']==pytest.approx(float(gross-fees-D('.014')))
    assert r['net_upper']==pytest.approx(float(gross-fees-D('.01')))


def test_unfavorable_entry_geometry_or_tiny_size_is_skipped():
    assert f.entry_quote(plan(),95,130,1000) is None
    assert f.entry_quote(plan(),95,94,1000) is None
    assert f.entry_quote(plan(),95,100,.000001) is None
    assert f.entry_quote(plan(),95,125,1000) is None


@pytest.mark.parametrize('direction',[1,-1])
def test_ambiguous_stop_target_and_adverse_gap_follow_existing_conservative_exit(direction):
    stop,target=(95,110) if direction==1 else (105,90)
    p=SimpleNamespace(direction=direction,stop=stop,target=target)
    assert _exit_trigger(SimpleNamespace(open=100,low=89,high=111),p)==(stop,'STOP_FIRST')
    raw=90 if direction==1 else 110
    assert _exit_trigger(SimpleNamespace(open=raw,low=raw-1,high=raw+1),p)==(raw,'STOP_GAP')


def test_utc_15m_aggregation_requires_exactly_three_official_5m_bars():
    rows=[{'t':i*300,'o':100+i,'c':101+i,'h':105+i,'l':99+i,'v':10} for i in range(3)]
    r=f.aggregate(rows,900)[0]
    assert r=={'t':0,'o':100,'c':103,'h':107,'l':99,'v':30}
    with pytest.raises(ValueError,match='Incomplete'):f.aggregate(rows[:2],900)


def test_future_tail_cannot_change_earlier_s1_context():
    start=f.source.EVALUATION_START
    data={}
    for tf,width in [('15m',900),('1h',3600),('4h',14400)]:
        data[tf]=[{'t':start+(i-80)*width,'o':100+i*.01,'c':100.1+i*.01,
                   'h':101+i*.01,'l':99+i*.01,'v':10} for i in range(83)]
    original=f.Context(data).at(start)[0]
    changed=copy.deepcopy(data)
    for tf in data:
        for r in changed[tf]:
            if r['t']>=start:
                for key in ('o','c','h','l'):r[key]*=10
    assert f.Context(changed).at(start)[0]==original


def test_execution_clock_assumption_is_strictly_after_observation():
    # Confirming 1m bar closes at 12:46; receipt/decision occur +1/+2s.
    observed=int(datetime(2023,11,15,12,46,2,tzinfo=timezone.utc).timestamp())
    entry=(observed//300+1)*300
    assert entry>observed
    assert datetime.fromtimestamp(entry,timezone.utc).minute==50


def test_execution_wrapper_can_fill_and_close_a_synthetic_candidate(monkeypatch):
    start=f.source.EVALUATION_START;end=start+15*86400
    minute=[{'t':t,'o':100,'c':100,'h':101,'l':99,'v':1}
            for t in range(start-120,end,60)]
    five=[{'t':t,'o':100,'c':100,'h':101,'l':99,'v':5}
          for t in range(start,end,300)]
    five[1]={**five[1],'h':135,'c':110}
    p={**plan(),'trigger':99,'scenario_id':'synthetic-order-wrapper-test'}
    issued=[]
    def create(*args):
        if issued:return None,'FIXTURE_NO_MORE_PLANS'
        issued.append(True);return p,'FIXTURE_CANDIDATE'
    monkeypatch.setattr(o,'create_plan',create)
    monkeypatch.setattr(o,'initial_state',lambda _: {'stage':'WAIT_BREAK'})
    monkeypatch.setattr(o,'advance',lambda *args,**kw:{'stage':'CANDIDATE_ONLY','stop':95,'reason':'SYNTHETIC_TEST_ONLY'})
    monkeypatch.setattr(o,'bias',lambda _: 'LONG')
    class SyntheticContext:
        def at(self,t):return None,SimpleNamespace(close=100),datetime.fromtimestamp(t+2,timezone.utc)
    result=f.replay({'1m':minute,'5m':five,'funding':{},'mark':{}},SyntheticContext(),0,'nominal')
    assert result['closed_trades']==1
    trade=result['trades'][0]
    assert trade['entry_t']==start+300 and trade['entry_t']>trade['candidate_at']
    assert trade['exit_reason']=='TARGET' and trade['net']>0 and trade['fees']>0
    assert result['counts']['FILLED_REFERENCE_ENTRY']==1
    assert not result['profitability_proven']
