"""Frozen SOL regime-long lead and matched-exit unfiltered entry control."""
from dataclasses import asdict,replace
from datetime import timezone

from app.strategy.gate_s4_complete_playbooks import Playbook,PlaybookDecision
from app.strategy.gate_s4_lab import digest
from app.strategy.gate_s4_regime_signal import features
from app.strategy.gate_s4_slow_momentum import MomentumReader,POLICY_ID as OLD_ID,planning_cost_fraction

CANDIDATE="SOL_REGIME_LONG_EXTERNAL_V1"
CONTROL="SOL_UNFILTERED_LONG_SAME_EXITS_V1"
ARMS=(CANDIDATE,CONTROL)
SYMBOLS=("SOLUSDT",)
POLICIES={a:Playbook(a,"1.0.0","POSITIVE_20D_OR_UNFILTERED",30,6,3,3.,"CASH") for a in ARMS}
WINDOWS=((253,3000),(3033,6000))


def manifest():
    return {"policies":{a:asdict(p) for a,p in POLICIES.items()},"symbol":"SOLUSDT","timeframe":"4h",
        "windows":[list(x) for x in WINDOWS],"warmup_bars":220,"purge_bars":33,
        "daily_decision_utc":"00:00","candidate_entry":"positive own120-bar log return",
        "control_entry":"LONG regardless of regime; identical20-day invalidation exit",
        "stop_atr":2.,"executed_reward_to_risk":3.,"base_entry_lag_bars":2,
        "nondegenerate_120_return_volatility_required":True,"planning_cost_fraction":planning_cost_fraction(),
        "planning_cost_over_target_max":.20,"maximum_entry_drift_atr":.5,
        "source":"same fixed lead as REGIME_LONG_CONTROL from prior study",
        "selected_after_prior_returns_seen":True,"parameter_grid_budget":0,"new_lead_budget":1,
        "independent_prospective_evidence":False,"execution_authorized":False}


class ExternalReader:
    def __init__(self,bars,*,cap):
        if not bars or bars[0].symbol!="SOLUSDT" or bars[0].timeframe!="4h" or cap>6000:
            raise ValueError("Only the frozen SOL4h external case is supported")
        self.base=MomentumReader(bars,cap=cap);self.bars,self.cap,self.cache=self.base.bars,cap,{}

    def momentum(self,count):return self.base.momentum(count)

    def decision(self,arm,count,clock):
        if arm not in ARMS:raise ValueError("Unregistered external policy")
        old=self.base.decision(OLD_ID,count,clock)  # Closed-prefix and clock validation.
        if (arm,count) in self.cache:return replace(self.cache[arm,count],available_at=clock)
        bar=self.bars[count-1];atr=old.atr
        d=PlaybookDecision(arm,"1.0.0",bar.symbol,bar.timeframe,clock,"WAIT",bar.close,atr,
            None,None,None,None,None,"NO_DAILY_OPPORTUNITY")
        stamp=bar.closed_at.astimezone(timezone.utc)
        if (stamp.hour,stamp.minute,stamp.second,stamp.microsecond)!=(0,0,0,0):return d
        if features(self.base,count) is None:return replace(d,reason="DEGENERATE_RETURN_VOLATILITY")
        if arm==CANDIDATE and self.momentum(count)<=0:return replace(d,reason="NONPOSITIVE_20D_REGIME")
        stop,target=bar.close-2*atr,bar.close+6*atr
        if atr<=0 or min(stop,target)<=0 or planning_cost_fraction()>.20*6*atr/bar.close:
            return replace(d,reason="UNCHANGED_PRICE_OR_COST_GEOMETRY_REJECTION")
        d=replace(d,action="LONG",stop_price=stop,target_price=target,
            invalidation_level=self.bars[count-121].close,setup_index=count-1,
            setup_id=digest([arm,bar.symbol,bar.closed_at.isoformat()]),reason="FROZEN_SOL_REGIME_LEAD_OR_ENTRY_CONTROL")
        self.cache[arm,count]=d;return d
