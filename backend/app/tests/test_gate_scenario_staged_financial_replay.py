from datetime import datetime,timezone
from types import SimpleNamespace

import pytest

from app.strategy import gate_scenario_staged_financial_replay as f


def synthetic(monkeypatch,scenario='nominal',expired=False):
    start=f.source.EVALUATION_START;end=start+15*86400
    minute=[{'t':t,'o':100,'c':100,'h':101,'l':99,'v':1} for t in range(start-120,end,60)]
    five=[{'t':t,'o':100,'c':100,'h':101,'l':99,'v':5} for t in range(start,end,300)]
    delay=f.SCENARIOS[scenario][1];five[(300+delay)//300]['h']=135
    p={'side':'LONG','policy':{'slippage_bps_side':f.SCENARIOS[scenario][0],
        'fee_bps_side':5,'minimum_net_reward_risk':2},'scenario_id':'synthetic-wrapper-only',
        'expires_at':datetime.fromtimestamp(start+(299 if expired else 14400),timezone.utc).isoformat()}
    issued=[]
    def create(*args):
        if issued:return None,'FIXTURE_NO_MORE_PLANS'
        issued.append(True);return p,'FIXTURE_PLAN'
    monkeypatch.setattr(f.o,'create_plan',create)
    monkeypatch.setattr(f.o,'initial_state',lambda _:{'stage':'WAIT_BREAK'})
    monkeypatch.setattr(f.o,'advance',lambda *a,**kw:{'stage':'CANDIDATE_ONLY','stop':95,
        'reason':'SYNTHETIC_ONLY','armed':{'trigger':99,'target':130,'level_id':'synthetic-level'},
        'candidate':{'synthetic_only':True}})
    monkeypatch.setattr(f.reference.o,'bias',lambda _:'LONG')
    class Context:
        def at(self,t):return None,SimpleNamespace(close=100),datetime.fromtimestamp(t+2,timezone.utc)
    return f.replay({'1m':minute,'5m':five,'funding':{},'mark':{}},Context(),0,scenario)


@pytest.mark.parametrize('scenario',['nominal','stress','stress_delayed'])
def test_staged_levels_become_future_entry_then_realized_synthetic_exit(monkeypatch,scenario):
    result=synthetic(monkeypatch,scenario)
    assert result['closed_trades']==1
    trade=result['trades'][0]
    assert trade['entry_t']==f.source.EVALUATION_START+300+f.SCENARIOS[scenario][1]
    assert trade['entry_t']>trade['candidate_at']
    assert trade['level_id']=='synthetic-level' and trade['exit_reason']=='TARGET'
    assert trade['net']>0 and trade['fees']>0
    assert result['counts']['FILLED_REFERENCE_ENTRY']==1
    assert result['scenario_events'][1]['armed']['target']==130
    assert not result['profitability_proven']


def test_expired_zone_cannot_enter_during_gap_between_last_observation_and_open(monkeypatch):
    result=synthetic(monkeypatch,expired=True)
    assert result['closed_trades']==0 and result['net_closed_point']==0
    assert result['counts']['ENTRY_EXPIRED_AT_EXECUTION_OPEN']==1


def test_reference_costs_and_calendar_are_not_relaxed_for_v2():
    rules=f.rules();old=f.reference.rules()
    for key in ('start','end','source_start','scenarios','approval','scenario_policy','risk'):
        assert rules[key]==old[key]
    assert rules['durable_input_checkpoint_before_run_required']
    assert rules['observer_version']==f.o.VERSION
