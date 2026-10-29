"""Independent Gate Testnet read-only connection status service."""
import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.demo.gate_readonly import GateProbeError, probe_account
from app.demo.gate_market import fetch_candles
from app.demo.gate_analysis import analyze_snapshot
from app.demo.gate_reconcile import inventory
from app.demo.gate_position_config import read_position_config

KEY_VARIABLE = "CAPITAL_CIPHER_GATE_TESTNET_API_KEY"
SECRET_VARIABLE = "CAPITAL_CIPHER_GATE_TESTNET_API_SECRET"


def create_app(environment=None, probe=probe_account, market_fetch=fetch_candles, research=analyze_snapshot,
               inventory_fn=inventory, position_config_fn=read_position_config):
    values = dict(os.environ if environment is None else environment)
    key, secret = values.get(KEY_VARIABLE, "").strip(), values.get(SECRET_VARIABLE, "").strip()
    state = {"environment": "GATE_TESTNET", "status": "WAITING_FOR_CREDENTIALS",
             "authenticated": False, "execution_enabled": False, "checked_at": None,
             "market_status": "CHECKING", "market_checked_at": None,
             "research_status": "WAITING" if values.get('GATE_RESEARCH_ANALYSIS_ENABLED') == 'true' else "DISABLED",
             "research_checked_at": None, "inventory_status":"WAITING", "inventory_checked_at":None,
             "position_config_status":"WAITING"}
    private_account = {'equity': None}

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
                        # No account amounts, credentials or agent details in logs.
                        logging.getLogger('uvicorn.error').info(
                            'GATE_RESEARCH_CANDLE %s AGENTS %d DECISION %s',
                            candle, int(result['total_agents']), result['decision']['candidate_action'])
                        last_candle = candle
                    state['research_status'] = 'AVAILABLE'
                except Exception:
                    state['research_status'] = 'UNAVAILABLE'
                state['research_checked_at'] = datetime.now(timezone.utc).isoformat()
                logging.getLogger('uvicorn.error').info('GATE_RESEARCH_STATUS %s', state['research_status'])
            await asyncio.sleep(300)

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
        tasks = [asyncio.create_task(monitor()), asyncio.create_task(market_monitor()), asyncio.create_task(inventory_monitor())]
        if values.get('GATE_RESEARCH_ANALYSIS_ENABLED') == 'true':
            tasks.append(asyncio.create_task(research_monitor()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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
        return {"status": "UP", "scope": "GATE_TESTNET_READ_ONLY"}

    @app.get("/api/gate/status")
    async def status():
        return dict(state)

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return PAGE

    return app


PAGE = """<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gate Testnet</title><style>body{background:#0c101a;color:#edf1fa;font:16px system-ui;margin:10vh auto;padding:24px;max-width:600px}section{background:#131b2b;border:1px solid #293349;border-radius:16px;padding:24px}p{line-height:1.6;color:#a5b1c8}strong{color:#f6ce67}</style><h1>Gate · Futuros Testnet</h1><section><p>Conexão: <strong id="status">Verificando...</strong></p><p>Dados de mercado: <strong id="market">Verificando...</strong></p><p>Análise dos agentes: <strong id="research">Verificando...</strong></p><p>Operações automáticas: <strong>Desativadas</strong></p><p>A consulta verifica a autenticação da conta de testes. Nenhuma ordem é enviada.</p></section><script>async function update(){try{const r=await fetch('/api/gate/status',{cache:'no-store'});if(!r.ok)throw 0;const s=await r.json();const l={WAITING_FOR_CREDENTIALS:'Aguardando chaves Testnet',INCOMPLETE_CREDENTIALS:'Chaves incompletas',CONNECTED_READ_ONLY:'Conectada · somente leitura',CONNECTION_FAILED:'Falha na conexão'};document.querySelector('#status').textContent=l[s.status]||'Verificando...';document.querySelector('#market').textContent=({CHECKING:'Verificando...',AVAILABLE:'Disponíveis',UNAVAILABLE:'Indisponíveis'})[s.market_status]||'Verificando...';document.querySelector('#research').textContent=({DISABLED:'Desativada',WAITING:'Aguardando dados',CHECKING:'Analisando...',AVAILABLE:'Ativa · sem ordens',UNAVAILABLE:'Indisponível'})[s.research_status]||'Verificando...'}catch(e){document.querySelector('#status').textContent='Indisponível'}}update();setInterval(update,10000)</script></html>"""

app = create_app()
