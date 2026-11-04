"""Independent Gate Testnet read-only connection status service."""
import asyncio
import logging
import math
import os
import time
from collections import deque
from contextlib import asynccontextmanager, AsyncExitStack
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from app.demo.gate_readonly import GateProbeError, probe_account
from app.demo.gate_market import fetch_candles
from app.demo.gate_stage1_shadow import professional_shadow_snapshot, GATE_S1_SYMBOLS
from app.strategy.gate_s2e_live_shadow import evaluate_shared_shadow
from app.demo.gate_s3_collector import GateS3Collector
from app.demo.gate_analysis import analyze_snapshot
from app.demo.gate_reconcile import inventory
from app.demo.gate_position_config import read_position_config
from app.demo.gate_research_journal import ResearchJournal
from app.demo.gate_cycle_journal import GateCycleJournal
from app.execution.gate_runtime import connect_gate_oms

KEY_VARIABLE = "CAPITAL_CIPHER_GATE_TESTNET_API_KEY"
SECRET_VARIABLE = "CAPITAL_CIPHER_GATE_TESTNET_API_SECRET"


def _shadow_delay(forward_enabled, now):
    if not forward_enabled:
        return 900
    target = math.floor(now / 900) * 900 + 10
    if target <= now:
        target += 900
    return max(0., target - now)


def create_app(environment=None, probe=probe_account, market_fetch=fetch_candles, research=analyze_snapshot,
               inventory_fn=inventory, position_config_fn=read_position_config,
               storage_path='/gate-data', mount_check=os.path.ismount, oms_connector=connect_gate_oms,
               stage1_shadow_runner=professional_shadow_snapshot,
               stage1_sleep=asyncio.sleep,
               strategy_shadow_runner=evaluate_shared_shadow,
               forward_research_factory=GateS3Collector):
    values = dict(os.environ if environment is None else environment)
    key, secret = values.get(KEY_VARIABLE, "").strip(), values.get(SECRET_VARIABLE, "").strip()
    state = {"environment": "GATE_TESTNET", "status": "WAITING_FOR_CREDENTIALS",
             "authenticated": False, "execution_enabled": False, "checked_at": None,
             "market_status": "CHECKING", "market_checked_at": None,
             "research_status": "WAITING" if values.get('GATE_RESEARCH_ANALYSIS_ENABLED') == 'true' else "DISABLED",
             "research_checked_at": None, "inventory_status":"WAITING", "inventory_checked_at":None,
             "position_config_status":"WAITING", "storage_status":"WAITING",
             "recovery_status":"WAITING"}
    # Independent research ONLY: no access to the OMS runtime, credentials or 300-agent decisions.
    state['professional_shadow'] = {
        'status': 'WAITING' if values.get('GATE_S1_SHADOW_ENABLED') == 'true' else 'DISABLED',
        'checked_at': None, 'markets': {}, 'execution_authorized': False,
        'cycle_count': 0, 'consecutive_ready_cycles': 0, 'recent_cycles': [],
    }
    # S2E is strictly opt-in AND requires the original read-only S1 monitor.
    # A standalone S2E=true flag without S1=true never starts anything.
    strategy_shadow_enabled = (
        values.get('GATE_S1_SHADOW_ENABLED')=='true' and
        values.get('GATE_S2E_SHADOW_ENABLED')=='true'
    )
    state['strategy_shadow'] = {
        'status':'WAITING' if strategy_shadow_enabled else 'DISABLED',
        'checked_at':None, 'markets':{}, 'execution_authorized':False,
        'source_decision_cohort_modified':False,
    }
    forward_research_enabled = (
        strategy_shadow_enabled and values.get('GATE_S3_SHADOW_ENABLED') == 'true')
    state['forward_research'] = {
        'status': 'WAITING' if forward_research_enabled else 'DISABLED',
        'execution_authorized': False, 'oms_touched': False,
        'profitability_proven': False,
    }
    forward = {'collector': None}
    shadow_cycles = deque(maxlen=16)
    shadow_cycle_count = 0
    shadow_ready_streak = 0
    private_account = {'equity': None}
    journals = {'research': None, 'cycle': None}
    runtime = {'flow':None,'status':'DISCONNECTED'}
    revision = values.get('RAILWAY_GIT_COMMIT_SHA', values.get('APP_SOURCE_REVISION', ''))
    # Separate public chart cache: never read/write S3 journals or execution state.
    chart_cache = {}
    chart_locks = {}

    async def refresh():
        private_account['equity'] = None
        state.update(authenticated=False, checked_at=None)
        if not key or not secret:
            state["status"] = "INCOMPLETE_CREDENTIALS" if key or secret else "WAITING_FOR_CREDENTIALS"
            state["checked_at"] = datetime.now(timezone.utc).isoformat()
            return
        try:
            account = await asyncio.to_thread(probe, key, secret)
        except GateProbeError:
            state["status"] = "CONNECTION_FAILED"
        except Exception:
            state["status"] = "CONNECTION_FAILED"
        else:
            state.update(status="CONNECTED_READ_ONLY", authenticated=True)
            try:
                equity = float(account['total'])
                if math.isfinite(equity) and equity > 0:
                    private_account['equity'] = equity
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        state["checked_at"] = datetime.now(timezone.utc).isoformat()

    async def monitor():
        while True:
            await refresh()
            await asyncio.sleep(60)

    async def refresh_market():
        try:
            rows = await market_fetch('BTCUSDT', '15m', limit=200)
            if len(rows) < 60:
                raise ValueError('insufficient candles')
        except Exception:
            state['market_status'] = 'UNAVAILABLE'
        else:
            state['market_status'] = 'AVAILABLE'
        state['market_checked_at'] = datetime.now(timezone.utc).isoformat()
        logging.getLogger('uvicorn.error').info('GATE_TESTNET_MARKET_STATUS %s', state['market_status'])

    async def market_monitor():
        while True:
            await refresh_market()
            await asyncio.sleep(300)

    async def research_monitor():
        last_candle = None
        while True:
            if not state['authenticated'] or state['market_status'] != 'AVAILABLE' or private_account['equity'] is None:
                state['research_status'] = 'WAITING'
                await asyncio.sleep(10)
                continue
            else:
                state['research_status'] = 'CHECKING'
                try:
                    result = await research('BTCUSDT', '15m', private_account['equity'])
                    candle = result['candle_closed_at']
                    if result['analysis_scope'] != 'RESEARCH_ONLY' or result['execution_enabled'] is not False:
                        raise ValueError('invalid scope')
                    if candle != last_candle:
                        if journals['research'] is None:
                            raise ValueError('durable research storage unavailable')
                        journals['research'].record(result, revision)
                        # No account amounts, credentials or agent details in logs.
                        logging.getLogger('uvicorn.error').info(
                            'GATE_RESEARCH_CANDLE %s AGENTS %d DECISION %s',
                            candle, int(result['total_agents']), result['decision']['candidate_action'])
                        if runtime['flow'] is not None:
                            await runtime['flow'].process_analysis(result)
                        last_candle = candle
                    state['research_status'] = 'AVAILABLE'
                except Exception as exc:
                    state['research_status'] = 'UNAVAILABLE'
                    logging.getLogger('uvicorn.error').warning(
                        'GATE_RESEARCH_FAILED error_type=%s stage=%s',
                        type(exc).__name__, getattr(runtime['flow'], 'analysis_stage', 'ANALYSIS'))
                state['research_checked_at'] = datetime.now(timezone.utc).isoformat()
                logging.getLogger('uvicorn.error').info('GATE_RESEARCH_STATUS %s', state['research_status'])
            await asyncio.sleep(300)

    async def professional_shadow_monitor():
        # Opt-in only. Runs separate from the OMS and 300-agent decision path.
        nonlocal shadow_cycle_count, shadow_ready_streak
        while True:
            cycle_started = time.perf_counter()
            shadow_cycle_count += 1
            try:
                summaries = {}
                strategy_summaries = {}
                for symbol in GATE_S1_SYMBOLS:
                    try:
                        if strategy_shadow_enabled:
                            kwargs = {'strategy_shadow_hook': strategy_shadow_runner}
                            if forward['collector'] is not None:
                                kwargs['observation_sink'] = forward['collector'].capture
                            report = await stage1_shadow_runner(symbol, **kwargs)
                        else:
                            report = await stage1_shadow_runner(symbol)
                        if (report.get('execution_authorized') is not False or
                                report.get('source_decision_cohort_modified') is not False or
                                report.get('agent_signal') not in {'NEUTRAL','WAIT'}):
                            raise ValueError('Unsafe Stage 1 SHADOW result')
                        summaries[symbol] = report
                        if strategy_shadow_enabled:
                            ext=report.get('strategy_shadow')
                            if (not isinstance(ext,dict)
                                    or ext.get('status')!='READ_ONLY_CONFLUENCE_READY'
                                    or ext.get('execution_authorized') is not False
                                    or ext.get('oms_touched') is not False
                                    or ext.get('source_decision_cohort_modified') is not False):
                                strategy_summaries[symbol] = {
                                    'status':'BLOCKED', 'execution_authorized':False,
                                }
                            else:
                                strategy_summaries[symbol] = {
                                    'status':'READ_ONLY_CONFLUENCE_READY',
                                    'candidate_count':int(ext.get('candidate_count',0)),
                                    'filtered_count':int(ext.get('filtered_count',0)),
                                    'execution_authorized':False,
                                }
                    except Exception as exc:
                        summaries[symbol] = {
                            'symbol': symbol, 'status': 'BLOCKED',
                            'error_type': type(exc).__name__,
                            'execution_authorized': False,
                        }
                        if strategy_shadow_enabled:
                            strategy_summaries[symbol]={
                                'status':'BLOCKED','execution_authorized':False,
                            }
                ready = sum(x.get('status') == 'READ_ONLY_CHART_READY'
                            for x in summaries.values())
                status = 'AVAILABLE' if ready == len(GATE_S1_SYMBOLS) else 'PARTIAL_OR_BLOCKED'
                shadow_ready_streak = shadow_ready_streak + 1 if status == 'AVAILABLE' else 0
                duration_ms = round(max(0., (time.perf_counter()-cycle_started)*1000), 3)
                checked = datetime.now(timezone.utc).isoformat()
                shadow_cycles.append({
                    'cycle': shadow_cycle_count,
                    'checked_at': checked,
                    'status': status,
                    'ready_markets': ready,
                    'total_markets': len(GATE_S1_SYMBOLS),
                    'duration_ms': duration_ms,
                })
                durations = sorted(c['duration_ms'] for c in shadow_cycles)
                def percentile(q):
                    from math import ceil
                    return durations[max(0, ceil(q * len(durations)) - 1)]
                state['professional_shadow'] = {
                    'status': status, 'checked_at': checked,
                    'markets': summaries, 'execution_authorized': False,
                    'cycle_count': shadow_cycle_count,
                    'consecutive_ready_cycles': shadow_ready_streak,
                    'recent_cycles': list(shadow_cycles),
                    'cycle_duration_ms': duration_ms,
                    'cycle_duration_p50_ms': percentile(.50),
                    'cycle_duration_p95_ms': percentile(.95),
                    'cycle_duration_p99_ms': percentile(.99),
                    'latency_samples': len(durations),
                    'prolonged_reliability_proven': False,
                    'predictive_edge_proven': False,
                }
                if strategy_shadow_enabled:
                    strategy_ready=sum(x['status']=='READ_ONLY_CONFLUENCE_READY'
                                       for x in strategy_summaries.values())
                    strategy_status=('AVAILABLE' if strategy_ready==len(GATE_S1_SYMBOLS)
                                     else 'PARTIAL_OR_BLOCKED')
                    state['strategy_shadow']={
                        'status':strategy_status,'checked_at':checked,
                        'markets':strategy_summaries,'execution_authorized':False,
                        'source_decision_cohort_modified':False,
                    }
                    logging.getLogger('uvicorn.error').info(
                        'GATE_S2E_SHADOW_STATUS %s ready_markets=%d total_markets=%d execution=false',
                        strategy_status,strategy_ready,len(GATE_S1_SYMBOLS),
                    )
                logging.getLogger('uvicorn.error').info(
                    'GATE_S1_SHADOW_STATUS %s ready_markets=%d total_markets=%d '
                    'execution=false cycle=%d streak=%d duration_ms=%.3f',
                    status, ready, len(GATE_S1_SYMBOLS),
                    shadow_cycle_count, shadow_ready_streak, duration_ms)
            except Exception:
                # Errors are isolated; preserve original Gate research and OMS.
                shadow_ready_streak = 0
                state['professional_shadow']['status'] = 'BLOCKED'
                state['professional_shadow']['checked_at'] = datetime.now(timezone.utc).isoformat()
                state['professional_shadow']['consecutive_ready_cycles'] = 0
                if strategy_shadow_enabled:
                    state['strategy_shadow']['status']='BLOCKED'
                    state['strategy_shadow']['checked_at']=datetime.now(timezone.utc).isoformat()
                logging.getLogger('uvicorn.error').warning('GATE_S1_SHADOW_MONITOR_BLOCKED')
            # Opt-in S3 uses confirmed 15m boundaries, avoiding accumulated
            # HTTP latency. The default S1-only cadence stays unchanged.
            await stage1_sleep(_shadow_delay(forward_research_enabled, time.time()))

    async def inventory_monitor():
        while True:
            if not state['authenticated']:
                state['inventory_status']='WAITING'
                await asyncio.sleep(10)
                continue
            try:
                result=await inventory_fn(key,secret)
                if result.get('execution_authorized') is not False or result.get('atomic') is not False:
                    raise ValueError('invalid inventory scope')
                state['inventory_status']='AVAILABLE'
                logging.getLogger('uvicorn.error').info('GATE_INVENTORY positions=%d orders=%d conditional_orders=%d atomic=false',
                    result['positions'],result['orders'],result['conditional_orders'])
            except Exception:
                state['inventory_status']='UNAVAILABLE'
                logging.getLogger('uvicorn.error').info('GATE_INVENTORY_UNAVAILABLE')
            state['inventory_checked_at']=datetime.now(timezone.utc).isoformat()
            try:
                config=await position_config_fn(key,secret)
                if config.get('execution_authorized') is not False or type(config.get('one_way_isolated_1x')) is not bool:
                    raise ValueError('invalid configuration scope')
                state['position_config_status']='ONE_WAY_ISOLATED_1X' if config['one_way_isolated_1x'] else 'REVIEW_REQUIRED'
                logging.getLogger('uvicorn.error').info('GATE_POSITION_CONFIG mode=%s margin=%s leverage=%s ready=%s',
                    config['mode'],config['margin'],config['leverage'],config['one_way_isolated_1x'])
            except Exception:
                state['position_config_status']='UNAVAILABLE'
                logging.getLogger('uvicorn.error').info('GATE_POSITION_CONFIG_UNAVAILABLE')
            await asyncio.sleep(300)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if not mount_check(storage_path):
                raise ValueError('persistent mount unavailable')
            directory=os.path.join(storage_path, 'capital-cipher-gate-testnet')
            os.makedirs(directory, mode=0o700, exist_ok=True)
            journals['research']=ResearchJournal(os.path.join(directory,'research.sqlite3'))
            journals['cycle']=GateCycleJournal(os.path.join(directory,'cycles.sqlite3'))
            recovery=journals['cycle'].recovery_status()
            state['storage_status']='AVAILABLE'
            state['recovery_status']='REQUIRED' if recovery['recovery_required'] else 'CLEAR'
        except Exception:
            state['storage_status']='UNAVAILABLE'
            state['recovery_status']='UNAVAILABLE'
            for journal in journals.values():
                if journal is not None:
                    journal.close()
            journals.update(research=None,cycle=None)
        if forward_research_enabled:
            try:
                if state['storage_status'] != 'AVAILABLE':
                    raise ValueError('S3 persistent mount unavailable')
                forward['collector'] = forward_research_factory(
                    storage_path, mount_verified=True)
                forward['collector'].start()
            except Exception:
                if forward['collector'] is not None:
                    try:
                        forward['collector'].stop(timeout=0)
                    except Exception:
                        pass
                forward['collector'] = None
                state['forward_research'].update(
                    status='BLOCKED', error_code='PERSISTENT_STORAGE_UNAVAILABLE')
        stack=AsyncExitStack()
        stop=asyncio.Event()
        try:
            runtime['flow']=await stack.enter_async_context(oms_connector(values,journals['cycle'],mount_verified=state['storage_status']=='AVAILABLE'))
            runtime['status']='CONNECTED' if runtime['flow'] is not None else 'DISCONNECTED'
        except Exception:
            runtime['status']='CONNECTION_FAILED'
        tasks = [asyncio.create_task(monitor()), asyncio.create_task(market_monitor()), asyncio.create_task(inventory_monitor())]
        if runtime['flow'] is not None:
            tasks.append(asyncio.create_task(runtime['flow'].run(stop)))
        if values.get('GATE_RESEARCH_ANALYSIS_ENABLED') == 'true':
            tasks.append(asyncio.create_task(research_monitor()))
        if values.get('GATE_S1_SHADOW_ENABLED') == 'true':
            tasks.append(asyncio.create_task(professional_shadow_monitor()))
        try:
            yield
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if forward['collector'] is not None:
                await asyncio.to_thread(forward['collector'].stop)
            await stack.aclose()
            for journal in journals.values():
                if journal is not None:
                    journal.close()

    app = FastAPI(title="Gate Testnet: leitura", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.refresh = refresh
    app.state.refresh_market = refresh_market

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    async def health():
        if values.get('GATE_OMS_CONNECTION_ENABLED')=='true' and runtime['flow'] is None:
            return JSONResponse({'status':'UNAVAILABLE','scope':'GATE_TESTNET_OMS'},status_code=503)
        return {"status": "UP", "scope": "GATE_TESTNET_OMS" if runtime["flow"] is not None and runtime["flow"].writes_enabled else "GATE_TESTNET_READ_ONLY"}

    @app.get("/api/gate/status")
    async def status():
        flow=runtime['flow']
        current=dict(state)
        current['source_revision'] = revision or None
        if forward['collector'] is not None:
            current['forward_research'] = forward['collector'].snapshot()['collection']
        current['oms_connection_status']=runtime['status']
        current['oms_flow_status']=flow.status if flow is not None else 'DISCONNECTED'
        current['oms_release_authorized']=getattr(flow,'release_authorized',False)
        current['oms_risk_clear']=getattr(flow,'central_risk_clear',False)
        if current['authenticated'] and flow is not None and flow.writes_enabled:
            current['status'] = 'CONNECTED_TESTNET'
        demo = getattr(flow, 'demo_release', None)
        current['demo_release'] = demo.status() if demo is not None else None
        current['canary_execution_enabled'] = bool(flow and flow.writes_enabled and demo is not None and demo.grant.mode == 'CANARY_ONLY' and demo.current() and getattr(flow,'release_authorized',False) and getattr(flow,'central_risk_clear',False) and flow.status not in {'AWAITING_OWNER_CANARY_ACCEPTANCE','CANARY_BUDGET_CONSUMED','CANARY_UNFILLED','TECHNICAL_CANARY_REVIEW_REQUIRED','HALTED','RECOVERY_REQUIRED'})
        current['execution_enabled']=bool((demo is None or demo.grant.mode == 'AGENTS' and demo.current() and demo.accepted()) and flow and flow.writes_enabled and getattr(flow,'release_authorized',False) and getattr(flow,'central_risk_clear',False) and flow.status in {'OPERATIONAL','PROTECTED','QUEUED','WAITING_FOR_SIGNAL'})
        if journals['cycle'] is not None:
            current['recovery_status']='REQUIRED' if journals['cycle'].recovery_status()['recovery_required'] else 'CLEAR'
        return current

    @app.get("/api/gate/market/candles")
    async def chart_candles(
        symbol: Literal['BTCUSDT', 'ETHUSDT', 'SOLUSDT'] = 'BTCUSDT',
        timeframe: Literal['15m', '1h', '4h'] = '15m',
    ):
        cache_key = (symbol, timeframe)
        lock = chart_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = chart_cache.get(cache_key)
            if cached is not None and time.monotonic() - cached[0] < 30:
                return cached[1]
            try:
                rows = await market_fetch(symbol, timeframe, limit=200)
                if len(rows) < 60:
                    raise ValueError('insufficient candles')
            except Exception:
                # No fallback to Binance, stale data, or a different account.
                raise HTTPException(502, 'GATE_CHART_DATA_UNAVAILABLE') from None
            result = {
                'source': 'GATE', 'environment': 'GATE_TESTNET',
                'market': 'GATE_USDT_FUTURES_TESTNET',
                'symbol': symbol, 'timeframe': timeframe,
                'observed_at': datetime.now(timezone.utc).isoformat(),
                'closed_candles_only': True, 'execution_authorized': False,
                'candles': rows,
            }
            chart_cache[cache_key] = (time.monotonic(), result)
            return result

    @app.get("/api/gate/research/forward")
    async def forward_status():
        # Cached, public-only report: HTTP clients never open or mutate SQLite.
        if forward['collector'] is None:
            result = {'collection': dict(state['forward_research']),
                      'comparison': None, 'execution_authorized': False}
            if state['forward_research']['status'] == 'BLOCKED':
                return JSONResponse(result, status_code=503)
            return result
        result = forward['collector'].snapshot()
        if result['collection']['status'] == 'BLOCKED':
            return JSONResponse(result, status_code=503)
        return result

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return PAGE

    return app


PAGE = """<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gate Testnet</title><style>body{background:#0c101a;color:#edf1fa;font:16px system-ui;margin:10vh auto;padding:24px;max-width:600px}section{background:#131b2b;border:1px solid #293349;border-radius:16px;padding:24px}p{line-height:1.6;color:#a5b1c8}strong{color:#f6ce67}</style><h1>Gate · Futuros Testnet</h1><section><p>Conexão: <strong id="status">Verificando...</strong></p><p>Dados de mercado: <strong id="market">Verificando...</strong></p><p>Análise dos agentes: <strong id="research">Verificando...</strong></p><p>Configuração BTC/USDT: <strong id="risk">Verificando...</strong></p><p>Registro persistente: <strong id="storage">Verificando...</strong></p><p>Recuperação de ciclos: <strong id="recovery">Verificando...</strong></p><p>Operações automáticas: <strong id="execution">Desativadas</strong></p><p>Conta Gate Testnet com fundos virtuais. O fluxo exige aprovação de risco e liberação da versão publicada.</p></section><script>async function update(){try{const r=await fetch('/api/gate/status',{cache:'no-store'});if(!r.ok)throw 0;const s=await r.json();document.querySelector('#execution').textContent=s.execution_enabled?'Ativas · Gate Testnet':({'ENTRY_BLOCKED':'Bloqueadas · liberação ou risco','CONNECTED_READ_ONLY':'Conectadas · envio desativado','HALTED':'Paradas · revisão necessária','RECOVERY_REQUIRED':'Paradas · conciliação necessária','CANARY_VALIDATING':'Validando uma operação demo','WAITING_FOR_CANARY_SIGNAL':'Aguardando sinal para validar uma operação','CANARY_BUDGET_CONSUMED':'Validação já consumida · revisar evidências','CANARY_UNFILLED':'Ordem sem execução · validação não concluída','TECHNICAL_CANARY_REVIEW_REQUIRED':'Teste técnico bloqueado · revisar risco ou fila','AWAITING_OWNER_CANARY_ACCEPTANCE':'Validação concluída · aguardando aceite do proprietário'})[s.oms_flow_status]||'Desativadas';const l={WAITING_FOR_CREDENTIALS:'Aguardando chaves Testnet',INCOMPLETE_CREDENTIALS:'Chaves incompletas',CONNECTED_READ_ONLY:'Conectada · somente leitura',CONNECTED_TESTNET:'Conectada · Gate Testnet',CONNECTION_FAILED:'Falha na conexão'};document.querySelector('#status').textContent=l[s.status]||'Verificando...';document.querySelector('#market').textContent=({CHECKING:'Verificando...',AVAILABLE:'Disponíveis',UNAVAILABLE:'Indisponíveis'})[s.market_status]||'Verificando...';document.querySelector('#research').textContent=({DISABLED:'Desativada',WAITING:'Aguardando dados',CHECKING:'Analisando...',AVAILABLE:'Ativa · sem ordens',UNAVAILABLE:'Indisponível'})[s.research_status]||'Verificando...';document.querySelector('#storage').textContent=({WAITING:'Verificando...',AVAILABLE:'Disponível',UNAVAILABLE:'Indisponível'})[s.storage_status]||'Verificando...';document.querySelector('#recovery').textContent=({WAITING:'Verificando...',CLEAR:'Sem ciclos pendentes',REQUIRED:'Revisão necessária',UNAVAILABLE:'Indisponível'})[s.recovery_status]||'Verificando...';document.querySelector('#risk').textContent=({WAITING:'Verificando...',ONE_WAY_ISOLATED_1X:'Isolada · 1x',REVIEW_REQUIRED:'Revisar margem e alavancagem',UNAVAILABLE:'Não foi possível verificar'})[s.position_config_status]||'Verificando...'}catch(e){document.querySelector('#status').textContent='Indisponível'}}update();setInterval(update,10000)</script></html>"""

app = create_app()

