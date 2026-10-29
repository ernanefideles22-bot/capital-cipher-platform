"""Durable virtual-funds execution with venue protections and bounded agent proposals."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_CEILING, ROUND_FLOOR

from app.demo.binance import DemoError
from app.demo.ledger import DemoLedger, utc
from app.demo.venue import VenueError


def number(value):
    result=Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise DemoError('DEMO_INVALID_NUMBER')
    return result


def text_number(value):
    return format(value, 'f')


def identity(kind, token):
    return 'cc'+kind+'-'+hashlib.sha256(token.encode()).hexdigest()[:28]


class DemoExecution:
    def __init__(self, venue, path, *, analysis_fn=None, entry_guard=None):
        self.venue=venue
        self.ledger=DemoLedger(path)
        self.lock=asyncio.Lock()
        self._analysis_fn=analysis_fn
        self._entry_guard=entry_guard
        self.analysis_lock=asyncio.Lock()
        self.canary_lock=asyncio.Lock()
        self.revision=os.getenv("APP_SOURCE_REVISION", "local")
        self.latest_analysis=None
        self.last_account=None
        self.last_error=None
        self.limits={'max_notional_usdt':200,'risk_per_trade_percent':.1,'max_open_positions':1,
            'leverage':1,'max_daily_drawdown_percent':2,'max_total_drawdown_percent':5,'max_consecutive_losses':3}

    def summary(self):
        return {'capability':'BINANCE_FUTURES_DEMO','execution_available':True,'automatic_enabled':bool(self.ledger.flag('automatic')),
            'halted':bool(self.ledger.flag('halted')),'canary_verified':self.canary_passed(),
            'limits':self.limits,'last_error':self.last_error,'trades':self.ledger.trades(),'events':self.ledger.events(),
            'analysis':self.latest_analysis,'observation':self.observation(),'live_execution_available':False}

    def observation(self):
        campaign=self.ledger.flag('observation')
        if not campaign:
            return None
        rows=self.ledger.observation_rows(campaign['id'])
        closed=[t for t in rows['trades'] if t['state']=='CLOSED']
        settled=[t for t in closed if t.get('realized_pnl_usdt') is not None]
        latest=rows['latest_analysis']
        account=self.last_account or {}
        return {**campaign,'source_revision':self.revision,
            'automatic_enabled':bool(self.ledger.flag('automatic')),'halted':bool(self.ledger.flag('halted')),
            'canary_verified':self.canary_passed(),'last_error':self.last_error,
            'analysis_count':rows['analysis_count'],'wait_count':rows['wait_count'],
            'risk_approved_count':rows['risk_approved_count'],
            'last_candle':latest['candle_closed_at'] if latest else None,
            'last_decision':{k:v for k,v in latest['decision'].items() if k in {'candidate_action','confidence','reason'}} if latest else None,
            'primary_agents':[{k:a.get(k) for k in ('agent_name','signal','status','confidence','reason')} for a in latest['agents'] if a['agent_name'] in latest['primary_agents']] if latest else [],
            'agent_status_counts':{status:sum(a['status']==status for a in latest['agents']) for status in {a['status'] for a in latest['agents']}} if latest else {},
            'trade_count':len(rows['trades']),'closed_count':len(closed),'settled_count':len(settled),'pending_pnl_count':len(closed)-len(settled),
            'net_realized_pnl_usdt':sum(t['realized_pnl_usdt'] for t in settled),
            'fees_usdt':sum(t.get('fees_usdt',0) for t in settled),
            'wins':sum(t['realized_pnl_usdt']>0 for t in settled),'losses':sum(t['realized_pnl_usdt']<0 for t in settled),
            'active_trade_count':len(self.ledger.active()),'equity_usdt':account.get('equity_usdt'),
            'unrealized_pnl_usdt':account.get('unrealized_pnl_usdt'),
            'account_checked_at':account.get('checked_at'),'attribution':'PRIMARY_ENSEMBLE_ONLY_SHADOW_NOT_CAUSAL'}

    def canary_passed(self):
        return bool(self.ledger.flag("canary_verified")) and self.ledger.flag("canary_revision")==self.revision

    async def account(self):
        self.last_account={**await self.venue.validate_account(),'checked_at':utc()}
        return self.last_account

    @staticmethod
    def _account_guard(account):
        if not account['authenticated'] or not account['one_way_mode'] or not account['single_asset_mode'] or not account['account_trading_enabled']:
            raise DemoError('DEMO_ACCOUNT_MODE_OR_PERMISSION_INVALID')
        if account['equity_usdt'] <= 0 or account['available_usdt'] <= 0:
            raise DemoError('DEMO_USDT_FUNDS_REQUIRED')

    def _drawdown_guard(self, account):
        equity=account['equity_usdt']; day=datetime.now(timezone.utc).date().isoformat()
        baseline=self.ledger.flag('equity_baseline') or equity
        peak=max(self.ledger.flag('equity_peak') or equity,equity)
        daily=self.ledger.flag('daily_baseline')
        if not daily or daily['day']!=day:
            daily={'day':day,'equity':equity}; self.ledger.flag('daily_baseline',daily)
        self.ledger.flag('equity_baseline',baseline);self.ledger.flag('equity_peak',peak)
        recent=[t for t in self.ledger.trades() if t['state']=='CLOSED' and t.get('source')!='OPERATIONAL_CANARY']
        losses=0
        for trade in recent:
            pnl=trade.get('realized_pnl_usdt')
            if pnl is None:
                break
            if pnl<0:
                losses+=1
            else:
                break
        if equity < daily['equity']*.98 or equity < peak*.95 or losses>=3:
            self.ledger.flag('halted',True); self.ledger.flag('automatic',False)
            raise DemoError('DEMO_DRAWDOWN_OR_LOSS_LIMIT')

    async def mark_price(self, symbol):
        row=await self.venue.request('GET','/fapi/v1/premiumIndex',{'symbol':symbol},public=True)
        if not isinstance(row,dict) or row.get('symbol')!=symbol:
            raise DemoError('DEMO_MARK_PRICE_INVALID')
        return number(row['markPrice'])

    async def _rules(self, symbol):
        info=await self.venue.request('GET','/fapi/v1/exchangeInfo',public=True)
        rows=[x for x in info.get('symbols',[]) if x['symbol']==symbol]
        if len(rows)!=1 or rows[0].get('quoteAsset')!='USDT' or rows[0].get('contractType')!='PERPETUAL' or rows[0].get('status')!='TRADING':
            raise DemoError('DEMO_INSTRUMENT_NOT_APPROVED')
        filters={f['filterType']:f for f in rows[0]['filters']}
        lot=filters.get('MARKET_LOT_SIZE',{})
        if Decimal(str(lot.get('stepSize',0)))<=0:
            lot=filters['LOT_SIZE']
        return {'step':number(lot['stepSize']),'min':number(lot['minQty']),'max':number(lot['maxQty']),
            'tick':number(filters['PRICE_FILTER']['tickSize']),
            'notional':number(filters['MIN_NOTIONAL'].get('notional',filters['MIN_NOTIONAL'].get('minNotional')))}

    async def analyze(self, symbol='ETHUSDT', timeframe='15m', *, canary_side=None, automatic=False):
        if symbol not in {'BTCUSDT','ETHUSDT','SOLUSDT'} or timeframe not in {'1m','5m','15m','1h','4h'}:
            raise DemoError('DEMO_ANALYSIS_SCOPE_INVALID')
        if self.analysis_lock.locked():
            raise DemoError('DEMO_ANALYSIS_ALREADY_RUNNING')
        async with self.analysis_lock:
            async with self.lock:
                account=await self.account();self._account_guard(account)
                candles=await self.venue.candles(symbol=symbol,timeframe=timeframe,limit=200)
                if len(candles)<60:
                    raise DemoError('DEMO_INSUFFICIENT_CANDLES')
                period={'1m':60,'5m':300,'15m':900,'1h':3600,'4h':14400}[timeframe]
                stamps=[int(r['close_time_ms']) for r in candles]
                age=datetime.now(timezone.utc).timestamp()*1000-stamps[-1]
                if not 0<=age<period*1000+30000 or any(b-a!=period*1000 for a,b in zip(stamps,stamps[1:])):
                    raise DemoError('DEMO_CANDLES_STALE_OR_GAPPED')
                price=float(await self.mark_price(symbol))
            payload={'symbol':symbol,'timeframe':timeframe,'candles':candles,'price':price,'equity':account['equity_usdt'],'canary_side':canary_side}
            if self._analysis_fn:
                result=await self._analysis_fn(payload)
            else:
                # Explicit environment whitelist: exchange/admin keys never reach agents.
                env={k:os.environ[k] for k in ('PATH','PYTHONPATH','LANG') if k in os.environ}
                env.update(PYTHONUNBUFFERED='1',PYTHONDONTWRITEBYTECODE='1')
                process=await asyncio.create_subprocess_exec(sys.executable,'-m','app.demo.analysis',stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env)
                try:
                    out,_=await asyncio.wait_for(process.communicate(json.dumps(payload).encode()),timeout=40)
                except BaseException:
                    if process.returncode is None:
                        process.kill();await process.wait()
                    raise DemoError('DEMO_AGENT_ANALYSIS_TIMEOUT') from None
                if process.returncode!=0 or len(out)>3_000_000:
                    raise DemoError('DEMO_AGENT_ANALYSIS_FAILED')
                try:
                    result=json.loads(out)
                except ValueError:
                    raise DemoError('DEMO_AGENT_OUTPUT_INVALID') from None
            if automatic and not canary_side and self.ledger.flag('automatic') and self.ledger.flag('observation'):
                result['observation_id']=self.ledger.flag('observation')['id']
            self.latest_analysis=self.ledger.proposal(result)
            self.ledger.event('AGENT_ANALYSIS',{'id':result['id'],'symbol':symbol,'action':result['decision']['candidate_action'],'agents':result['total_agents']})
            return result

    async def execute(self, proposal_id):
        async with self.lock:
            existing=self.ledger.trade(proposal_id)
            if existing:
                return existing
            if self.ledger.flag('halted'):
                raise DemoError('DEMO_KILL_SWITCH_ACTIVE')
            if self._entry_guard:
                await self._entry_guard()
            proposal=self.ledger.get_proposal(proposal_id)
            campaign=self.ledger.flag('observation')
            if proposal.get('observation_id') and (not campaign or proposal['observation_id']!=campaign['id'] or datetime.fromisoformat(campaign['expires_at'])<=datetime.now(timezone.utc)):
                raise DemoError('DEMO_OBSERVATION_EXPIRED')
            risk=proposal.get('risk') or {}
            if not risk.get('approved') or not risk.get('approval_id') or risk.get('risk_status') not in {'APPROVED','REDUCED'}:
                raise DemoError('DEMO_CENTRAL_RISK_NOT_APPROVED')
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(risk['created_at'])).total_seconds()
            if not 0<=age<=60:
                raise DemoError('DEMO_APPROVAL_EXPIRED')
            await self._reconcile_locked()
            if self.ledger.active():
                raise DemoError('DEMO_POSITION_OR_UNRESOLVED_ORDER_EXISTS')
            account=await self.account();self._account_guard(account);self._drawdown_guard(account)
            if account['positions'] or account['open_order_count'] or account['open_algo_order_count']:
                raise DemoError('DEMO_ACCOUNT_NOT_FLAT')
            symbol=proposal['symbol'];side=proposal['decision']['candidate_action']
            if symbol not in {'BTCUSDT','ETHUSDT','SOLUSDT'} or side not in {'BUY','SELL'} or risk.get('leverage')!=1:
                raise DemoError('DEMO_ORDER_SCOPE_INVALID')
            mark=await self.mark_price(symbol); rules=await self._rules(symbol)
            reference=number(proposal['reference_price'])
            if abs(mark/reference-1)>.005:
                raise DemoError('DEMO_ENTRY_PRICE_MOVED')
            stop=number(risk['stop_loss']);take=number(risk['take_profit']);tick=rules['tick']
            stop=(stop/tick).to_integral_value(rounding=ROUND_CEILING if side=='BUY' else ROUND_FLOOR)*tick
            take=(take/tick).to_integral_value(rounding=ROUND_FLOOR if side=='BUY' else ROUND_CEILING)*tick
            if not (stop<mark<take if side=='BUY' else take<mark<stop):
                raise DemoError('DEMO_PROTECTION_BRACKET_INVALID')
            budget=min(number(risk['position_size']),Decimal('200'),number(account['available_usdt'])*Decimal('.2'))
            qty=(budget/mark/rules['step']).to_integral_value(rounding=ROUND_DOWN)*rules['step']
            if qty<rules['min'] or qty>rules['max'] or qty*mark<rules['notional']:
                raise DemoError('DEMO_VENUE_MINIMUM_OR_QUANTITY_LIMIT')
            cost=mark*Decimal('.0014')
            if qty*(abs(mark-stop)+cost)>number(account['equity_usdt'])*Decimal('.001') or (abs(take-mark)-cost)/(abs(mark-stop)+cost)<Decimal('1.3'):
                raise DemoError('DEMO_NET_RISK_REWARD_OR_RISK_BUDGET')
            trade={'id':proposal_id,'state':'RESERVED','symbol':symbol,'side':side,'quantity':text_number(qty),
                'stop_loss':text_number(stop),'take_profit':text_number(take),'reference_price':float(mark),
                'notional_usdt':float(qty*mark),'leverage':1,'source':proposal['source'],'approval_id':risk['approval_id'],
                'risk_check':risk,'signal_key':f"{symbol}:{proposal['timeframe']}:{proposal['candle_closed_at']}:{proposal['source']}:{side}" + (':'+proposal_id if proposal['source']=='OPERATIONAL_CANARY' else ''),
                'created_at':utc(),'realized_pnl_usdt':None}
            if proposal.get('observation_id'):
                trade['observation_id']=proposal['observation_id']
            self.ledger.reserve(trade)
            try:
                # Configuration is idempotent, but ambiguous writes still halt the intent.
                await self.venue.request('POST','/fapi/v1/leverage',{'symbol':symbol,'leverage':1})
                try:
                    await self.venue.request('POST','/fapi/v1/marginType',{'symbol':symbol,'marginType':'ISOLATED'})
                except VenueError as exc:
                    if exc.venue_code!=-4046:
                        raise
                config=await self.venue.request('GET','/fapi/v1/symbolConfig',{'symbol':symbol})
                if len(config)!=1 or config[0].get('symbol')!=symbol or int(config[0]['leverage'])!=1 or config[0].get('marginType')!='ISOLATED':
                    raise DemoError('DEMO_LEVERAGE_OR_MARGIN_NOT_CONFIRMED')
                if self.ledger.flag('halted'):
                    raise DemoError('DEMO_KILL_SWITCH_ACTIVE')
                if self._entry_guard:
                    await self._entry_guard()
                if proposal.get('observation_id') and datetime.fromisoformat(self.ledger.flag('observation')['expires_at'])<=datetime.now(timezone.utc):
                    self.ledger.flag('automatic',False)
                    raise DemoError('DEMO_OBSERVATION_EXPIRED')
                self.ledger.update(proposal_id,'ENTRY_SENDING')
                await self.venue.request('POST','/fapi/v1/order',{'symbol':symbol,'side':side,'positionSide':'BOTH','type':'MARKET',
                    'quantity':text_number(qty),'newClientOrderId':identity('en',proposal_id),'newOrderRespType':'RESULT'})
                # Confirm using the order lookup schema. A successful POST can
                # return a different acknowledgement shape on the Demo venue.
                row=await self.venue.request('GET','/fapi/v1/order',{'symbol':symbol,'origClientOrderId':identity('en',proposal_id)})
                if row.get('status')!='FILLED' or row.get('clientOrderId')!=identity('en',proposal_id) or row.get('symbol')!=symbol or row.get('side')!=side or Decimal(row.get('executedQty','0'))!=qty:
                    raise VenueError('DEMO_ENTRY_NOT_FULLY_CONFIRMED',ambiguous=True)
                self.ledger.update(proposal_id,'ENTRY_ACKNOWLEDGED',entry_order_id=str(row['orderId']),entry_price=float(number(row['avgPrice'])))
                await self._protect(proposal_id)
                return self.ledger.update(proposal_id,'PROTECTED')
            except Exception as exc:
                current=self.ledger.trade(proposal_id)
                code=exc.code if isinstance(exc,DemoError) else 'DEMO_EXECUTION_FAILED'
                self.last_error=code;self.ledger.flag('automatic',False)
                # Exception values may contain signed URLs; retain only the
                # class and application source line for bounded diagnostics.
                frames=[];tb=exc.__traceback__
                while tb:
                    if '/app/demo/' in tb.tb_frame.f_code.co_filename:
                        frames.append({'function':tb.tb_frame.f_code.co_name,'line':tb.tb_lineno})
                    tb=tb.tb_next
                self.ledger.event('EXECUTION_FAULT',{'id':proposal_id,'error_type':type(exc).__name__,'frames':frames,'venue_code':exc.venue_code if isinstance(exc,VenueError) else None})
                if current['state']=='RESERVED':
                    self.ledger.update(proposal_id,'REJECTED',error_code=code)
                else:
                    self.ledger.update(proposal_id,'UNKNOWN',error_code=code);self.ledger.flag('halted',True)
                    # If the entry was acknowledged, protection failure is followed
                    # by one reduce-only emergency exit. Ambiguous entries are read/reconciled.
                    if current.get('entry_order_id'):
                        try:
                            await self._close_locked(proposal_id,reason='PROTECTION_FAILED')
                        except Exception:
                            pass
                raise DemoError(code) from None

    async def _algo(self, trade_id, kind):
        try:
            return await self.venue.request('GET','/fapi/v1/algoOrder',{'clientAlgoId':identity(kind,trade_id)})
        except VenueError as exc:
            if exc.venue_code in {-2013,-2011}:
                return None
            raise

    async def _protect(self, trade_id):
        trade=self.ledger.trade(trade_id)
        opposite='SELL' if trade['side']=='BUY' else 'BUY'
        for kind,order_type,price in [('sl','STOP_MARKET',trade['stop_loss']),('tp','TAKE_PROFIT_MARKET',trade['take_profit'])]:
            existing=await self._algo(trade_id,kind)
            if existing is None:
                if trade.get(kind+'_sent'):
                    raise DemoError('DEMO_PROTECTION_WRITE_UNRESOLVED')
                self.ledger.update(trade_id,'PROTECTING',**{kind+'_sent':True})
                await self.venue.request('POST','/fapi/v1/algoOrder',{'symbol':trade['symbol'],'side':opposite,'positionSide':'BOTH',
                    'algoType':'CONDITIONAL','type':order_type,'triggerPrice':price,'closePosition':'true',
                    'workingType':'MARK_PRICE','clientAlgoId':identity(kind,trade_id)})
                existing=await self._algo(trade_id,kind)
            if not existing or existing.get('algoStatus') not in {'NEW','WORKING'} or existing.get('symbol')!=trade['symbol'] or existing.get('clientAlgoId')!=identity(kind,trade_id) or existing.get('orderType')!=order_type or existing.get('positionSide')!='BOTH' or existing.get('side')!=opposite or existing.get('closePosition') is not True or existing.get('workingType')!='MARK_PRICE' or number(existing['triggerPrice'])!=number(price):
                raise DemoError('DEMO_PROTECTION_NOT_CONFIRMED')
            self.ledger.update(trade_id,'PROTECTING',**{kind+'_algo_id':str(existing['algoId'])})

    async def close_trade(self, trade_id):
        async with self.lock:
            return await self._close_locked(trade_id,reason='MANUAL_CLOSE')

    async def _close_locked(self, trade_id, *, reason):
        trade=self.ledger.trade(trade_id)
        if not trade:
            raise DemoError('DEMO_TRADE_NOT_FOUND')
        if trade['state']=='CLOSED':
            return trade
        account=await self.account()
        positions=[p for p in account['positions'] if p['symbol']==trade['symbol']]
        if positions:
            if len(positions)!=1 or positions[0]['position_side']!='BOTH' or positions[0]['side']!=trade['side']:
                raise DemoError('DEMO_EXIT_POSITION_IDENTITY_INVALID')
            if trade.get('exit_sent'):
                # Read-only recovery, never repeat an ambiguous exit write.
                row=await self.venue.request('GET','/fapi/v1/order',{'symbol':trade['symbol'],'origClientOrderId':identity('ex',trade_id)})
                if row.get('status')!='FILLED':
                    raise DemoError('DEMO_EXIT_UNRESOLVED')
            else:
                self.ledger.update(trade_id,'CLOSING',exit_sent=True,reason=reason)
                try:
                    row=await self.venue.request('POST','/fapi/v1/order',{'symbol':trade['symbol'],
                        'side':'SELL' if trade['side']=='BUY' else 'BUY','positionSide':'BOTH','type':'MARKET',
                        'quantity':text_number(number(positions[0]['quantity'])),'reduceOnly':'true',
                        'newClientOrderId':identity('ex',trade_id),'newOrderRespType':'RESULT'})
                except Exception:
                    self.ledger.flag('automatic',False);self.ledger.flag('halted',True)
                    self.ledger.update(trade_id,'UNKNOWN',error_code='DEMO_EXIT_UNKNOWN')
                    raise DemoError('DEMO_EXIT_UNKNOWN') from None
            if row.get('status')!='FILLED' or row.get('clientOrderId')!=identity('ex',trade_id):
                raise DemoError('DEMO_EXIT_NOT_CONFIRMED')
            self.ledger.update(trade_id,'CLOSING',exit_order_id=str(row['orderId']))
            account=await self.account()
            if any(p['symbol']==trade['symbol'] for p in account['positions']):
                raise DemoError('DEMO_EXIT_POSITION_REMAINS')
        await self._cleanup(trade_id)
        pnl=await self._pnl(trade_id)
        return self.ledger.update(trade_id,'CLOSED',closed_at=utc(),realized_pnl_usdt=pnl)

    async def _cleanup(self, trade_id):
        for kind in ('sl','tp'):
            algo=await self._algo(trade_id,kind)
            if algo and algo.get('algoStatus') in {'NEW','WORKING'}:
                await self.venue.request('DELETE','/fapi/v1/algoOrder',{'algoId':algo['algoId']})
                remaining=await self._algo(trade_id,kind)
                if remaining and remaining.get('algoStatus') in {'NEW','WORKING'}:
                    raise DemoError('DEMO_PROTECTION_CLEANUP_UNCONFIRMED')

    async def _pnl(self, trade_id):
        trade=self.ledger.trade(trade_id)
        if not trade.get('exit_order_id'):
            try:
                recovered=await self.venue.request('GET','/fapi/v1/order',{'symbol':trade['symbol'],'origClientOrderId':identity('ex',trade_id)})
                if recovered.get('status')=='FILLED':
                    trade=self.ledger.update(trade_id,trade['state'],exit_order_id=str(recovered['orderId']))
            except VenueError as exc:
                if exc.venue_code not in {-2011,-2013}:
                    raise
        ids={str(trade[k]) for k in ('entry_order_id','exit_order_id') if trade.get(k)}
        for kind in ('sl','tp'):
            algo=await self._algo(trade_id,kind)
            if algo and algo.get('actualOrderId'):
                ids.add(str(algo['actualOrderId']))
        rows=await self.venue.request('GET','/fapi/v1/userTrades',{'symbol':trade['symbol'],'limit':1000})
        fills=[r for r in rows if str(r['orderId']) in ids]
        if not trade.get('entry_order_id') or not fills or any(r.get('commissionAsset')!='USDT' for r in fills):
            return None
        # Missing exit fills must not be presented as a closed-trade profit.
        exits=[r for r in fills if str(r['orderId'])!=trade.get('entry_order_id')]
        entries=[r for r in fills if str(r['orderId'])==trade['entry_order_id']]
        if sum(float(r['qty']) for r in exits)+1e-12<float(trade['quantity']) or sum(float(r['qty']) for r in entries)+1e-12<float(trade['quantity']):
            return None
        gross=sum(float(r['realizedPnl']) for r in fills)
        fees=sum(float(r['commission']) for r in fills)
        value=gross-fees
        if not all(math.isfinite(v) for v in (gross,fees,value)):
            return None
        self.ledger.update(trade_id,trade['state'],gross_realized_pnl_usdt=gross,fees_usdt=fees)
        return value

    async def reconcile(self):
        async with self.lock:
            return await self._reconcile_locked()

    async def _reconcile_locked(self):
        account=await self.account()
        for trade in self.ledger.active():
            positions=[p for p in account['positions'] if p['symbol']==trade['symbol']]
            if trade['state'] in {'ENTRY_SENDING','UNKNOWN'} and not trade.get('entry_order_id'):
                try:
                    row=await self.venue.request('GET','/fapi/v1/order',{'symbol':trade['symbol'],'origClientOrderId':identity('en',trade['id'])})
                except VenueError as exc:
                    if exc.venue_code in {-2011,-2013} and not positions:
                        self.ledger.update(trade['id'],'REJECTED',reason='VENUE_CONFIRMED_NO_ENTRY')
                        continue
                    raise
                if row.get('status')=='FILLED':
                    self.ledger.update(trade['id'],'RECOVERY_REQUIRED',entry_order_id=str(row['orderId']),entry_price=float(number(row['avgPrice'])))
                    await self._close_locked(trade['id'],reason='RECOVERED_AMBIGUOUS_ENTRY')
                self.ledger.flag('automatic',False);self.ledger.flag('halted',True)
            elif not positions:
                await self._cleanup(trade['id'])
                self.ledger.update(trade['id'],'CLOSED',closed_at=utc(),realized_pnl_usdt=await self._pnl(trade['id']))
            elif trade['state'] in {'PROTECTED','ENTRY_ACKNOWLEDGED','PROTECTING','RECOVERY_REQUIRED','UNKNOWN'} and trade.get('entry_order_id') and not trade.get('exit_sent'):
                try:
                    await self._protect(trade['id'])
                    self.ledger.update(trade['id'],'PROTECTED')
                except Exception:
                    self.ledger.flag('halted',True);self.ledger.flag('automatic',False)
                    await self._close_locked(trade['id'],reason='PROTECTION_MISSING')
            else:
                self.ledger.flag('halted',True);self.ledger.flag('automatic',False)
        account=await self.account()
        managed={t['symbol'] for t in self.ledger.active()}
        if any(p['symbol'] not in managed for p in account['positions']):
            self.ledger.flag('halted',True);self.ledger.flag('automatic',False)
            raise DemoError('DEMO_UNMANAGED_POSITION')
        return {'account':account,'execution':self.summary()}

    async def kill(self):
        self.ledger.flag('halted',True);self.ledger.flag('automatic',False)
        self.ledger.event('KILL_SWITCH',{})
        results=[]
        for trade in self.ledger.active():
            results.append(await self.close_trade(trade['id']))
        return {'halted':True,'closed':results}

    async def set_automatic(self, enabled):
        if enabled:
            if self.ledger.flag('halted') or not self.canary_passed():
                raise DemoError('DEMO_CANARY_REQUIRED_OR_KILL_SWITCH_ACTIVE')
            async with self.lock:
                if self._entry_guard:
                    await self._entry_guard()
                await self._reconcile_locked()
                if self.ledger.active() or self.ledger.flag('halted'):
                    raise DemoError('DEMO_ACTIVE_TRADE_OR_HALT_EXISTS')
                account=await self.account();self._account_guard(account);self._drawdown_guard(account)
                campaign=self.ledger.flag('observation')
                if not campaign or datetime.fromisoformat(campaign['expires_at'])<=datetime.now(timezone.utc):
                    started=datetime.now(timezone.utc)
                    campaign={'id':identity('obs',utc()),'started_at':started.isoformat(),
                        'expires_at':(started+timedelta(hours=48)).isoformat(),'baseline_equity_usdt':account['equity_usdt'],
                        'symbol':'ETHUSDT','timeframe':'15m','starting_revision':self.revision}
                    self.ledger.flag('observation',campaign);self.ledger.event('OBSERVATION_STARTED',campaign)
        self.ledger.flag('automatic',bool(enabled));self.ledger.event('AUTOMATIC_MODE',{'enabled':bool(enabled)})
        return self.summary()

    async def cycle(self):
        campaign=self.ledger.flag('observation')
        if self.ledger.flag('automatic') and campaign and datetime.fromisoformat(campaign['expires_at'])<=datetime.now(timezone.utc):
            self.ledger.flag('automatic',False);self.ledger.event('OBSERVATION_EXPIRED',{'id':campaign['id']})
        if not self.ledger.active() and not self.ledger.flag('automatic'):
            pending=[t for t in self.ledger.trades()[:10] if t['state']=='CLOSED' and t.get('entry_order_id') and t.get('realized_pnl_usdt') is None]
            if pending:
                async with self.lock:
                    await self.venue.sync_clock()
                    for trade in pending:
                        pnl=await self._pnl(trade['id'])
                        if pnl is not None:
                            self.ledger.update(trade['id'],'CLOSED',realized_pnl_usdt=pnl)
            return
        await self.reconcile()
        if not self.ledger.flag('automatic') or self.ledger.active() or self.ledger.flag('halted'):
            return
        if self.analysis_lock.locked():
            return
        # Check the venue's latest closed candle before running the cohort or
        # writing a proposal. Repeated monitor ticks must not fill the volume.
        async with self.lock:
            candles=await self.venue.candles(symbol='ETHUSDT',timeframe='15m',limit=2)
        if not candles:
            return
        closed_at=datetime.fromtimestamp(candles[-1]['close_time_ms']/1000,timezone.utc).isoformat()
        if self.ledger.flag('last_auto_candle')==closed_at:
            return
        proposal=await self.analyze(automatic=True)
        candle=proposal['candle_closed_at']
        if self.ledger.flag('last_auto_candle')==candle:
            return
        self.ledger.flag('last_auto_candle',candle)
        if (proposal.get('risk') or {}).get('approved'):
            await self.execute(proposal['id'])

    async def canary(self):
        if self.canary_lock.locked():
            raise DemoError('DEMO_CANARY_ALREADY_RUNNING')
        async with self.canary_lock:
            return await self._canary()

    async def _canary(self):
        if self.ledger.active() or self.ledger.flag('automatic'):
            raise DemoError('DEMO_ACTIVE_TRADE_OR_AUTOMATION_EXISTS')
        results=[]
        for side in ('BUY','SELL'):
            proposal=await self.analyze(canary_side=side)
            trade=await self.execute(proposal['id'])
            if trade['state']!='PROTECTED':
                raise DemoError('DEMO_CANARY_PROTECTION_FAILED')
            closed=await self.close_trade(trade['id'])
            results.append({'side':side,'trade_id':trade['id'],'protected':True,'closed':closed['state']=='CLOSED'})
        async with self.lock:
            account=await self.account()
            if account['positions'] or account['open_order_count'] or account['open_algo_order_count']:
                raise DemoError('DEMO_CANARY_NOT_FLAT')
        self.ledger.flag('canary_verified',True);self.ledger.flag('canary_revision',self.revision);self.ledger.event('CANARY_PASSED',{'legs':results})
        return {'passed':True,'legs':results,'flat':True,'automatic_enabled':False}
