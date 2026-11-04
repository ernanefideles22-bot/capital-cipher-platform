"""One fixed V2 economic reference; reuse verified V1 accounting, never orders."""
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
import hashlib

from app.strategy import gate_scenario_financial_replay as reference
from app.strategy import gate_scenario_staged_levels as o

source, Context, load_sources = reference.source, reference.Context, reference.load_sources
SCENARIOS, as_candle = reference.SCENARIOS, reference.as_candle
entry_quote, signed_funding = reference.entry_quote, reference.signed_funding
_exit_trigger = reference._exit_trigger


def rules():
    value=reference.rules()
    value.update(version='SCENARIO_STAGED_LEVELS_FINANCIAL_REFERENCE_V2',
        observer_sha256=hashlib.sha256(Path(o.__file__).read_bytes()).hexdigest(),
        observer_version=o.VERSION,observer_rule_modified=True,
        observer_rule='isolated frozen staged-levels V2 from PR102; no tuning',
        hypothesis='post-hoc development comparison on reused V1 dates; not independent confirmation',
        entry_deadline='cancel if frozen zone expires before execution OPEN',
        durable_input_checkpoint_before_run_required=True,
        frozen_core_commit='a45114b105d3a1be89cd7f4529160e9c87538afe')
    return value


def close_trade(position,raw,reason,t):
    return {**reference.close_trade(position,raw,reason,t),'level_id':position['level_id']}


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
                if (reference.o.bias(reading)!=pending['plan']['side']
                        or (m.low<=pending['stop'] if sign==1 else m.high>=pending['stop'])
                        or (m.high>=pending['plan']['target'] if sign==1 else m.low<=pending['plan']['target'])
                        or sign*(m.close-pending['plan']['trigger'])<=0
                        or now>=o.clock(pending['plan']['expires_at'])):
                    counts['PENDING_ENTRY_INVALIDATED']+=1;pending=None
            if plan is not None:
                before=state['stage']
                state=o.advance(plan,state,reading,bar15,m,as_of=now)
                if state['stage']!=before:
                    counts[state['stage']+':'+state['reason']]+=1
                    events.append({'as_of':now.isoformat(),'scenario_id':plan['scenario_id'],
                                   'stage':state['stage'],'reason':state['reason'],
                                   'armed':state['armed'],'stop':state['stop'],'candidate':state['candidate']})
                if state['stage']=='CANDIDATE_ONLY':
                    available=int(now.timestamp())
                    armed=state['armed']
                    execution_reference={k:plan[k] for k in ('side','policy','scenario_id','expires_at')}
                    execution_reference.update(trigger=armed['trigger'],target=armed['target'],
                                               level_id=armed['level_id'],execution_authorized=False)
                    pending={'plan':execution_reference,'stop':state['stop'],'candidate_at':available,
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
            if t>=int(o.clock(pending['plan']['expires_at']).timestamp()):
                counts['ENTRY_EXPIRED_AT_EXECUTION_OPEN']+=1
            elif equity<day_start*.98 or equity<peak*.9:
                counts['ENTRY_BLOCKED_BY_RESEARCH_RISK']+=1
            else:
                quoted=entry_quote(pending['plan'],pending['stop'],bar.open,equity)
                if quoted is None:counts['ENTRY_GEOMETRY_OR_LOT_REJECTED']+=1
                else:
                    assert t>pending['candidate_at']
                    position={**quoted,'entry_t':t,'candidate_at':pending['candidate_at'],
                              'scenario_id':pending['plan']['scenario_id'],'level_id':pending['plan']['level_id']}
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
