"""One fixed pooled ridge forecast; fit-only scaling, causal regime and controls."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import timedelta, timezone
from math import isfinite, log, sqrt
from statistics import fmean, pstdev

from app.strategy.gate_classical_s2 import _atr
from app.strategy.gate_s4_complete_playbooks import Playbook, PlaybookDecision
from app.strategy.gate_s4_lab import digest
from app.strategy.gate_s4_slow_momentum import (
    MomentumReader, POLICY_ID as OLD_ID, SYMBOLS, planning_cost_fraction,
)

MODEL = "REGIME_RIDGE_4H_V1"
ARMS = (MODEL, "MEAN_FORECAST_CONTROL", "MOMENTUM20_CONTROL", "REGIME_LONG_CONTROL")
POLICIES = {a: Playbook(a,"1.0.0","FIXED_20D_INVALIDATION",30,6,3,3.,"CASH") for a in ARMS}
FEATURES = ("return_1d_normalized", "return_5d_normalized", "return_20d_normalized",
            "log_volatility_1d_over_20d", "efficiency_20d", "return_1d_x_20d")
RULES = {"fit_end":1200,"evaluation_windows":[[1233,2400],[2433,3600]],"purge_bars":33,
    "label_horizon_bars":30,"label_entry_lag_bars":2,"ridge_penalty_mean_objective":.1,
    "feature_clip":5.,"minimum_fit_rows":120,"minimum_fit_rows_per_market":40,
    "feature_lookbacks":[6,30,120],"daily_hour_utc":0,
    "minimum_absolute_forecast":planning_cost_fraction(),"forecast_direction_must_match_20d":True,
    "parameter_grid_budget":0,"model_fit_budget":1,"model_refits_during_evaluation":0}


def manifest():
    return {"arms":list(ARMS),"features":list(FEATURES),"rules":RULES,
        "policies":{a:asdict(p) for a,p in POLICIES.items()},
        "fitted_forecast_is_tradable_pnl":False,"automatic_agent_registration":False,
        "known_project_history_reused":True,"independent_validation":False,
        "execution_authorized":False}


def _clip(v):
    return max(-5.,min(5.,v))


def features(reader, count):
    reader.momentum(count)  # Validate the bounded prefix.
    prices=[b.close for b in reader.bars[count-121:count]]
    returns=[log(b/a) for a,b in zip(prices,prices[1:])]
    sigma=pstdev(returns)
    if sigma<=1e-12:
        return None
    trend=[_clip(log(prices[-1]/prices[-1-n])/(sigma*sqrt(n))) for n in (6,30,120)]
    vol=_clip(log(max(pstdev(returns[-6:]),1e-12)/sigma))
    distance=sum(abs(v) for v in returns)
    efficiency=abs(sum(returns))/distance if distance else 0.
    return (*trend,vol,efficiency,trend[0]*trend[2])


def _solve(a,b):
    n=len(b);m=[list(row)+[value] for row,value in zip(a,b)]
    for j in range(n):
        pivot=max(range(j,n),key=lambda i:abs(m[i][j]))
        if abs(m[pivot][j])<1e-12:raise ValueError("Singular ridge system")
        m[j],m[pivot]=m[pivot],m[j]
        value=m[j][j];m[j]=[v/value for v in m[j]]
        for i in range(n):
            if i!=j:
                factor=m[i][j];m[i]=[x-factor*y for x,y in zip(m[i],m[j])]
    return tuple(row[-1] for row in m)


@dataclass(frozen=True)
class Ridge:
    means: tuple
    scales: tuple
    coefficients: tuple
    constant_forecast: float
    training_rows: int
    max_label_index: int
    fit_rows_sha256: str

    def predict(self, x):
        if len(x)!=6 or not all(isfinite(v) for v in x):raise ValueError("Six finite features required")
        z=[_clip((v-m)/s) for v,m,s in zip(x,self.means,self.scales)]
        return self.coefficients[0]+sum(a*b for a,b in zip(self.coefficients[1:],z))


def fit_ridge(rows):
    if (len(rows)<RULES["minimum_fit_rows"] or any(len(r["x"])!=6 or
            not all(isfinite(v) for v in (*r["x"],r["y"])) or
            r["symbol"] not in SYMBOLS or r["label_index"]!=r["decision_index"]+31 or
            not 220<=r["decision_index"]<r["label_index"]<1200 for r in rows)):
        raise ValueError("Bounded finite fit labels required before evaluation")
    if any(sum(r["symbol"]==s for r in rows)<40 for s in SYMBOLS):
        raise ValueError("At least forty fit observations per registered market required")
    means=tuple(fmean(r["x"][j] for r in rows) for j in range(6))
    scales=tuple(max(pstdev(r["x"][j] for r in rows),1e-12) for j in range(6))
    x=[(1.,*(_clip((v-m)/s) for v,m,s in zip(r["x"],means,scales))) for r in rows]
    a=[[sum(row[j]*row[k] for row in x)+(len(rows)*.1 if j==k and j>0 else 0.)
        for k in range(7)] for j in range(7)]
    b=[sum(row[j]*r["y"] for row,r in zip(x,rows)) for j in range(7)]
    return Ridge(means,scales,_solve(a,b),fmean(r["y"] for r in rows),len(rows),
        max(r["label_index"] for r in rows),digest(rows))


def training_rows(datasets):
    rows=[]
    for symbol in sorted(SYMBOLS):
        bars=datasets[symbol][:1200];reader=MomentumReader(bars,cap=1200)
        for i in range(220,1200):
            stamp=bars[i].closed_at.astimezone(timezone.utc)
            if stamp.hour!=0 or stamp.minute or stamp.second or stamp.microsecond or i+31>=1200:continue
            x=features(reader,i+1)
            if x is not None:
                rows.append({"symbol":symbol,"decision_index":i,"label_index":i+31,
                    "x":list(x),"y":bars[i+31].close/bars[i+2].open-1})
    return rows


class RegimeReader:
    def __init__(self,bars,*,cap,model):
        if type(cap) is not int or cap>3600:raise ValueError("Only known training prefixes through 3600")
        self.base=MomentumReader(bars,cap=cap)
        self.bars,self.cap,self.model,self.cache=self.base.bars,cap,model,{}

    def momentum(self,count):return self.base.momentum(count)

    def decision(self,arm,count,clock):
        if arm not in ARMS or count-1<1233 or self.model.max_label_index>=count-1:
            raise ValueError("Forecast/control before matured fit labels or evaluation start")
        old=self.base.decision(OLD_ID,count,clock)  # Validates policy clock and prefix.
        if (arm,count) in self.cache:return replace(self.cache[arm,count],available_at=clock)
        if arm=="MOMENTUM20_CONTROL":
            d=replace(old,policy_id=arm,setup_id=digest([arm,count]) if old.setup_id else None)
            self.cache[arm,count]=d;return d
        bar=self.bars[count-1];atr=_atr(self.bars[count-15:count])
        d=PlaybookDecision(arm,"1.0.0",bar.symbol,bar.timeframe,clock,"WAIT",bar.close,atr,
            None,None,None,None,None,"NO_DAILY_FORECAST_OPPORTUNITY")
        stamp=bar.closed_at.astimezone(timezone.utc)
        if (stamp.hour,stamp.minute,stamp.second,stamp.microsecond)!=(0,0,0,0):return d
        x=features(self.base,count)
        if x is None:return replace(d,reason="DEGENERATE_FEATURE_VOLATILITY")
        mu=self.model.predict(x) if arm==MODEL else self.model.constant_forecast
        direction=1 if (arm=="REGIME_LONG_CONTROL" or mu>0) else -1
        if arm!="REGIME_LONG_CONTROL" and abs(mu)<planning_cost_fraction():
            return replace(d,reason="FORECAST_BELOW_FIXED_COST_PROXY")
        if direction*self.momentum(count)<=0:
            return replace(d,reason="FORECAST_NOT_ALIGNED_WITH_FIXED_20D_REGIME")
        stop,target=bar.close-direction*2*atr,bar.close+direction*6*atr
        if atr<=0 or min(stop,target)<=0 or planning_cost_fraction()>.2*6*atr/bar.close:
            return replace(d,reason="UNCHANGED_PRICE_OR_COST_GEOMETRY_REJECTION")
        d=replace(d,action="LONG" if direction==1 else "SHORT",stop_price=stop,target_price=target,
            invalidation_level=self.bars[count-121].close,setup_index=count-1,
            setup_id=digest([arm,bar.symbol,bar.closed_at.isoformat()]),reason="FIXED_REGIME_FORECAST_OR_CONTROL")
        self.cache[arm,count]=d;return d
