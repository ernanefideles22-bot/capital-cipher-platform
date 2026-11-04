"""Fixed economic wrapper around the unchanged scenario observer. No orders."""
from bisect import bisect_right
from collections import Counter
from dataclasses import replace, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from app.schemas.market import Candle
from app.strategy import gate_scenario_observer as o
from app.strategy import gate_scenario_profitability_audit as source
from app.strategy.gate_s2_replay import ResearchRisk, _exit_trigger
from app.strategy.professional_gate_chart_stage1 import analyze_gate_multi_timeframe

SCENARIOS = {'nominal': (6.,0), 'stress': (12.,0), 'stress_delayed': (12.,300)}
STEPS = {'1m':60,'5m':300,'15m':900,'1h':3600,'4h':14400}


def rules():
    return {'version':'SCENARIO_FINANCIAL_REFERENCE_V1','observer_sha256':source.OBSERVER_SHA256,
        'symbol':'BTC_USDT','source_start':source.SOURCE_START,
        'start':source.EVALUATION_START,'end':source.END,'calendar_blocks':2,'block_days':15,
        'scenarios':SCENARIOS,'lookback_each_frame':80,'observer_rule_modified':False,
        'scenario_policy':asdict(o.Policy()),'fees_bps_side':5,
        'execution':'first complete 5m OPEN strictly after candidate availability; scenario delay added',
        'receipt_lag_seconds_assumed':1,'decision_lag_seconds_assumed':2,
        'historical_receipts_verified':False,'missing_1m':'cancel scenario and unfilled entry; never impute',
        'open_position_missing_1m':'resting bracket simulated on independent complete official 5m OHLC',
        'ambiguous_bar':'stop first; target gap uses target price; adverse stop gap uses open',
        'max_holding_seconds':21600,'cooldown_seconds':3600,
        'risk':asdict(ResearchRisk()),'assumed_base_lot':.0001,'historical_lots_verified':False,
        'funding':'settled rates * fixed base quantity * 4h mark open; adjacent candle conditional bounds',
        'funding_at_entry_or_open_exit':'point zero; bounds include zero and full signed payment',
        'ending_open_position':'forced close estimate; exclude from closed-trade sample',
        'approval':{'min_closed_trades_each_case':30,'closed_net_and_marked_lower_positive':True,
            'profit_factor_at_least':1.2,'largest_win_share_at_most':.5,'close_dd_at_most_pct':5},
        'parameter_search_budget':0,'financial_case_budget':6,'execution_authorized':False}


def as_candle(r,tf):
    close=datetime.fromtimestamp(r['t']+STEPS[tf],timezone.utc)
    return Candle(exchange='GATE',symbol='BTCUSDT',timeframe=tf,open=r['o'],high=r['h'],
        low=r['l'],close=r['c'],volume=r['v'],closed_at=close,received_at=close+timedelta(seconds=1))


def aggregate(rows,width):
    grouped={}
    for r in rows:
        grouped.setdefault(r['t']//width*width,[]).append(r)
    out=[]
    for t,values in sorted(grouped.items()):
        if [v['t'] for v in values] != list(range(t,t+width,300)):
            raise ValueError('Incomplete 5m -> 15m aggregate')
        out.append({'t':t,'o':values[0]['o'],'c':values[-1]['c'],
                    'h':max(v['h'] for v in values),'l':min(v['l'] for v in values),
                    'v':sum(v['v'] for v in values)})
    return out


def load_sources(old_root,root):
    old_root,root=Path(old_root),Path(root)
    prior=source.audit(old_root)  # Preserves the old audit and checks all five receipts.
    data={}
    intent=json.loads((root/'source-supplement-intent.json').read_text())
    declared=datetime.fromisoformat(intent['declared_at_utc'])
    if intent['new_public_get_budget'] != 6 or intent['source_months'] != ['202311','202312']:
        raise ValueError('Fixed supplement intent required')
    hashes={}
    for tf in ('5m','1h','4h'):
        rows=[]
        for month in ('202311','202312'):
            name=f'candlesticks_{tf}-{month}.csv.gz';p=root/'raw-supplement'/name
            raw=p.read_bytes();receipt=json.loads(p.with_name(name+'.receipt.json').read_text())
            expected=f'https://download.gatedata.org/futures_usdt/candlesticks_{tf}/{month}/BTC_USDT-{month}.csv.gz'
            if (receipt['url']!=expected or receipt['status']!=200 or len(raw)!=receipt['bytes']
                    or hashlib.sha256(raw).hexdigest()!=receipt['sha256']
                    or not declared<=datetime.fromisoformat(receipt['requested_at_utc'])<=datetime.fromisoformat(receipt['received_at_utc'])):
                raise ValueError('Supplement receipt mismatch')
            hashes[name]=receipt['sha256']
            hashes[name+'.receipt.json']=hashlib.sha256(p.with_name(name+'.receipt.json').read_bytes()).hexdigest()
            rows.extend(source.parse_prices(raw,month))
        rows=[r for r in rows if source.SOURCE_START<=r['t']<source.END]
        if [r['t'] for r in rows]!=list(range(source.SOURCE_START,source.END,STEPS[tf])):
            raise ValueError('Complete supplemental grid required')
        data[tf]=rows
    minute=[];funding=[]
    for month in ('202311','202312'):
        minute.extend(source.parse_prices((old_root/'raw'/f'candlesticks_1m-{month}.csv.gz').read_bytes(),month))
        funding.extend(source.parse_funding((old_root/'raw'/f'funding_applies-{month}.csv.gz').read_bytes(),month))
    data['1m']=[r for r in minute if source.SOURCE_START<=r['t']<source.END]
    data['15m']=aggregate(data['5m'],900)
    data['funding']={r['t']:r['r'] for r in funding if source.SOURCE_START<=r['t']<=source.END}
    data['mark']={r['t']:r for r in json.loads((old_root/'raw/mark-4h.json').read_text())}
    return data,{'prior_source_intent_sha256':prior['source_intent_sha256_unchanged'],
        'supplement_intent_sha256':hashlib.sha256((root/'source-supplement-intent.json').read_bytes()).hexdigest(),
        'prior_raw_hashes':prior['archived_file_sha256'],'supplement_raw_hashes':hashes,
        'rows':{k:len(v) for k,v in data.items()},'base_source_minutes_missing':69,
        'new_market_requests_by_replay':0}


class Context:
    def __init__(self,data):
        self.bars={tf:[as_candle(r,tf) for r in data[tf]] for tf in ('15m','1h','4h')}
        self.closes={tf:[int(b.closed_at.timestamp()) for b in bars] for tf,bars in self.bars.items()}
        self.cache={}

    def at(self,closed):
        indices={tf:bisect_right(times,closed) for tf,times in self.closes.items()}
        if any(i<80 for i in indices.values()):raise ValueError('Insufficient initial warmup')
        key=tuple(indices.values())
        now=datetime.fromtimestamp(closed+2,timezone.utc)
        if key not in self.cache:
            snapshot={tf:bars[indices[tf]-80:indices[tf]] for tf,bars in self.bars.items()}
            self.cache[key]=analyze_gate_multi_timeframe(snapshot,as_of=now)
        original=self.cache[key]
        reading=replace(original,as_of=now,readings=tuple(replace(r,as_of=now) for r in original.readings))
        return reading,self.bars['15m'][indices['15m']-1],now


def signed_funding(quantity,direction,rate,current,previous,*,boundary=False):
    factor=-direction*quantity*rate
    point=factor*float(current['o'])
    low,high=sorted((factor*min(float(current['l']),float(previous['l'])),
                     factor*max(float(current['h']),float(previous['h']))))
    return (0.,min(0.,low),max(0.,high)) if boundary else (point,low,high)


def entry_quote(plan,stop,raw,equity):
    values=o.economics(plan,raw,stop)
    sign=1 if plan['side']=='LONG' else -1
    if (sign*(raw-stop)<=0 or sign*(plan['target']-raw)<=0
            or values['net_risk_per_base_unit']<=0 or values['net_reward_per_base_unit']<=0
            or values['net_reward_risk']<plan['policy']['minimum_net_reward_risk']):
        return None
    slip=plan['policy']['slippage_bps_side']/10000
    fill=raw*(1+sign*slip)
    risk=ResearchRisk()
    q=min(equity*risk.risk_per_trade_pct/100/values['net_risk_per_base_unit'],equity/fill)
    q=float((Decimal(str(q))/Decimal('.0001')).to_integral_value(rounding=ROUND_DOWN)*Decimal('.0001'))
    return None if q<=0 else {'entry':fill,'quantity':q,'direction':sign,'stop':stop,
        'target':plan['target'],'entry_fee':fill*q*.0005,'slip':slip,'funding':0.,'funding_low':0.,
        'funding_high':0.,'funding_events':[],'planned_reward_risk':values['net_reward_risk']}


def close_trade(position,raw,reason,t):
    p=position;exit_price=raw*(1-p['direction']*p['slip'])
    gross=p['quantity']*p['direction']*(exit_price-p['entry'])
    fees=p['entry_fee']+exit_price*p['quantity']*.0005
    return {**{k:p[k] for k in ('entry','quantity','direction','stop','target','entry_t',
                                'candidate_at','scenario_id','funding_events','planned_reward_risk')},
        'exit':exit_price,'exit_bar_open_t':t,'exit_reason':reason,'gross_after_slippage':gross,'fees':fees,
        'funding_cash':p['funding'],'funding_lower':p['funding_low'],'funding_upper':p['funding_high'],
        'net':gross-fees+p['funding'],'net_lower':gross-fees+p['funding_low'],
        'net_upper':gross-fees+p['funding_high'],'actual_fill_verified':False}


def replay(data,context,block,scenario):
    if block not in (0,1) or scenario not in SCENARIOS:raise ValueError('Fixed replay budget')
    slip,delay=SCENARIOS[scenario]
    policy=o.Policy(slippage_bps_side=slip)
    start=source.EVALUATION_START+block*15*86400;end=start+15*86400
    minute={r['t']:r for r in data['1m']}
    five={r['t']:r for r in data['5m']}
    equity=peak=day_start=1000.;dd=0.;day=None;position=pending=plan=state=None
    trades=[];events=[];counts=Counter();cooldown=0;last_lower=1000.
    for t in range(start,end,60):
        # Observation ending t-60 was received at t-59, strictly BEFORE this OPEN.
        observed=minute.get(t-120);closed=t-60
        reading,bar15,now=context.at(closed)
        if observed is None:
            counts['MISSING_MINUTE_NO_IMPUTATION']+=1
            if plan is not None:counts['ACTIVE_SCENARIO_CANCELLED_ON_GAP']+=1
            if pending is not None:counts['UNFILLED_ENTRY_CANCELLED_ON_GAP']+=1
            plan=state=pending=None
        elif position is None:
            m=as_candle(observed,'1m')
            if pending is not None:
                sign=1 if pending['plan']['side']=='LONG' else -1
                if (o.bias(reading)!=pending['plan']['side']
                        or (m.low<=pending['stop'] if sign==1 else m.high>=pending['stop'])
                        or (m.high>=pending['plan']['target'] if sign==1 else m.low<=pending['plan']['target'])
                        or sign*(m.close-pending['plan']['trigger'])<=0):
                    counts['PENDING_ENTRY_INVALIDATED']+=1;pending=None
            if plan is not None:
                before=state['stage']
                state=o.advance(plan,state,reading,bar15,m,as_of=now)
                if state['stage']!=before:
                    counts[state['stage']+':'+state['reason']]+=1
                    events.append({'as_of':now.isoformat(),'scenario_id':plan['scenario_id'],
                                   'stage':state['stage'],'reason':state['reason']})
                if state['stage']=='CANDIDATE_ONLY':
                    available=int(now.timestamp())
                    pending={'plan':plan,'stop':state['stop'],'candidate_at':available,
                             'enter_at':(available//300+1)*300+delay}
                if state['stage'] in o.TERMINAL:plan=state=None
            elif pending is None and closed%900==0 and t>=cooldown:
                plan,reason=o.create_plan(reading,bar15.close,policy)
                counts['PLAN:'+reason]+=1
                if plan is not None:
                    state=o.initial_state(plan)
                    events.append({'as_of':now.isoformat(),'stage':'WAIT_TOUCH','plan':plan})
        if t%300:continue
        bar=as_candle(five[t],'5m')
        utcday=t//86400
        if utcday!=day:day,day_start=utcday,equity
        if position is None and pending is not None and t>=pending['enter_at']:
            if equity<day_start*.98 or equity<peak*.9:
                counts['ENTRY_BLOCKED_BY_RESEARCH_RISK']+=1
            else:
                quoted=entry_quote(pending['plan'],pending['stop'],bar.open,equity)
                if quoted is None:counts['ENTRY_GEOMETRY_OR_LOT_REJECTED']+=1
                else:
                    assert t>pending['candidate_at']
                    position={**quoted,'entry_t':t,'candidate_at':pending['candidate_at'],
                              'scenario_id':pending['plan']['scenario_id']}
                    counts['FILLED_REFERENCE_ENTRY']+=1
            pending=None
        if position is not None:
            bracket=SimpleNamespace(**position)
            trigger=_exit_trigger(bar,bracket)
            at_open=trigger is not None and trigger[1] in ('STOP_GAP','TARGET_GAP_LIMIT')
            if not at_open and t-position['entry_t']>=21600:trigger=(bar.open,'TIME_LIMIT');at_open=True
            if t in data['funding']:
                mark_t=t//14400*14400
                payment=signed_funding(position['quantity'],position['direction'],data['funding'][t],
                    data['mark'][mark_t],data['mark'][mark_t-14400],boundary=at_open or t==position['entry_t'])
                position['funding']+=payment[0];position['funding_low']+=payment[1];position['funding_high']+=payment[2]
                position['funding_events'].append({'t':t,'rate':data['funding'][t],
                    'point':payment[0],'lower':payment[1],'upper':payment[2],'boundary_ambiguous':at_open or t==position['entry_t']})
            if trigger is not None:
                trade=close_trade(position,*trigger,t);trades.append(trade);equity+=trade['net']
                position=None;cooldown=t+3600
        if position is None:last_lower=1000+sum(r['net_lower'] for r in trades)
        else:
            exit_mark=bar.close*(1-position['direction']*position['slip'])
            last_lower=(1000+sum(r['net_lower'] for r in trades)+position['quantity']*position['direction']*(exit_mark-position['entry'])
                -position['entry_fee']-exit_mark*position['quantity']*.0005+position['funding_low'])
        peak=max(peak,last_lower);dd=max(dd,(1-last_lower/peak)*100)
    if position is not None:
        trade=close_trade(position,five[end-300]['c'],'END_WINDOW_ESTIMATE',end-300)
        trades.append(trade);equity+=trade['net']
    closed_trades=[r for r in trades if r['exit_reason']!='END_WINDOW_ESTIMATE']
    net=sum(r['net'] for r in closed_trades);lower=sum(r['net_lower'] for r in closed_trades)
    wins=[r['net_lower'] for r in closed_trades if r['net_lower']>0]
    losses=[-r['net_lower'] for r in closed_trades if r['net_lower']<0]
    pf=sum(wins)/sum(losses) if losses else None
    share=max(wins)/sum(wins) if wins else None
    marked_lower=sum(r['net_lower'] for r in trades)
    reasons=[]
    for failed,reason in [(len(closed_trades)<30,'INSUFFICIENT_CLOSED_TRADES'),
        (lower<=0 or marked_lower<=0,'NONPOSITIVE_NET'),(pf is None or pf<1.2,'PROFIT_FACTOR'),
        (share is None or share>.5,'CONCENTRATED_OR_ABSENT_GAINS'),(dd>5,'DRAWDOWN')]:
        if failed:reasons.append(reason)
    return {'block':block+1,'scenario':scenario,'start':start,'end':end,'closed_trades':len(closed_trades),
        'net_closed_point':net,'net_closed_lower':lower,'net_marked_point':equity-1000,
        'expectancy_point':net/len(closed_trades) if closed_trades else None,
        'return_marked_pct':(equity/1000-1)*100,'capital_each_block':1000,
        'total_fees':sum(r['fees'] for r in trades),'total_funding_cash':sum(r['funding_cash'] for r in trades),
        'net_marked_lower':marked_lower,'profit_factor_lower':pf,'largest_win_share_lower':share,
        'win_rate_point':sum(r['net']>0 for r in closed_trades)/len(closed_trades) if closed_trades else None,
        'max_5m_close_drawdown_lower_pct':dd,'counts':dict(counts),'trades':trades,
        'scenario_events':events,'screen_passed':not reasons,'reasons':reasons,
        'profitability_proven':False,'execution_authorized':False}
