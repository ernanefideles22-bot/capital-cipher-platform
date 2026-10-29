"""Isolated Futures Demo account, analysis and protected execution boundary."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Literal
from app.demo.execution import DemoExecution
from app.demo.venue import FuturesDemoVenue
from fastapi.responses import HTMLResponse, JSONResponse

from app.demo.binance import (
    BinanceFuturesDemoClient, DemoCredentials, DemoError, KEY_VARIABLE, SECRET_VARIABLE,
)

ACKNOWLEDGEMENT = "BINANCE_FUTURES_DEMO_ONLY_NO_REAL_FUNDS"
FORBIDDEN_CONNECTIONS = (
    "DATABASE_URL", "REDIS_URL", "MIGRATION_DATABASE_URL",
    "CAPITAL_CIPHER_BINANCE_TESTNET_KEY_ID", "CAPITAL_CIPHER_BINANCE_TESTNET_SIGNING_SECRET",
    "CAPITAL_CIPHER_BYBIT_TESTNET_KEY_ID", "CAPITAL_CIPHER_BYBIT_TESTNET_SIGNING_SECRET",
    "BINANCE_API_KEY", "BINANCE_API_SECRET",
)


def validate_environment(values):
    if values.get("DEMO_ACKNOWLEDGEMENT") != ACKNOWLEDGEMENT:
        raise RuntimeError("DEMO_ACKNOWLEDGEMENT_REQUIRED")
    if values.get("DEMO_RESOURCE_NAMESPACE") != "capital-cipher-binance-futures-demo":
        raise RuntimeError("DEMO_RESOURCE_NAMESPACE_INVALID")
    if any(values.get(name, "").strip() for name in FORBIDDEN_CONNECTIONS):
        raise RuntimeError("DEMO_ISOLATION_VIOLATION")
    execution = values.get("DEMO_EXECUTION_ENABLED", "false").lower()
    if execution not in {"true", "false"}:
        raise RuntimeError("DEMO_EXECUTION_FLAG_INVALID")
    if execution == "true" and (values.get("DEMO_WRITE_ACKNOWLEDGEMENT") != "VIRTUAL_FUNDS_ONLY_PROTECTED_FUTURES" or values.get("DEMO_STATE_ROOT") != "/var/lib/capital-cipher-demo"):
        raise RuntimeError("DEMO_EXECUTION_ISOLATION_REQUIRED")
    admin = values.get("DEMO_ADMIN_API_KEY", "")
    if len(admin) < 32 or len(set(admin)) < 8:
        raise RuntimeError("DEMO_ADMIN_KEY_REQUIRED")
    key, secret = values.get(KEY_VARIABLE, ""), values.get(SECRET_VARIABLE, "")
    if bool(key) != bool(secret):
        raise RuntimeError("DEMO_CREDENTIAL_PAIR_INCOMPLETE")
    return DemoCredentials(key, secret) if key and secret else None


def create_app(*, environment=None, transport=None, clock_ms=None, status_clock=None):
    values = dict(os.environ if environment is None else environment)
    credentials = validate_environment(values)
    admin_key = values["DEMO_ADMIN_API_KEY"]
    now = status_clock or (lambda: datetime.now(timezone.utc))
    enabled = values.get("DEMO_EXECUTION_ENABLED", "false").lower() == "true"
    client_type = FuturesDemoVenue if enabled else BinanceFuturesDemoClient
    client = client_type(credentials, transport=transport, clock_ms=clock_ms)
    async def central_entry_guard():
        # Only read central controls; this boundary never accesses PAPER storage.
        try:
            async with httpx.AsyncClient(timeout=8,trust_env=False,follow_redirects=False) as controls:
                response=await controls.get('http://capital-cipher-backend.railway.internal:8080/api/v1/risk/status')
            payload=response.json()
            data=payload['data']
            if response.status_code!=200 or payload.get('success') is not True or data.get('kill_switch_active') is not False or data.get('durable_control_active') is not False:
                raise DemoError('DEMO_CENTRAL_CONTROL_BLOCKED')
        except DemoError:
            raise
        except Exception:
            raise DemoError('DEMO_CENTRAL_CONTROL_UNAVAILABLE') from None
    executor = DemoExecution(client, values["DEMO_STATE_ROOT"]+"/execution.sqlite3",entry_guard=central_entry_guard) if enabled else None
    state = {
        "status": "CHECKING", "market": "USD_M_FUTURES_DEMO", "credentials_present": credentials is not None,
        "authenticated": False, "market_reachable": False, "account_preflight_passed": False,
        "execution_enabled": False, "live_execution_available": False,
        "checked_at": None, "error_code": None, "issues": [], "validation_in_progress": False,
        "source_revision": values.get("APP_SOURCE_REVISION", "unknown"),
    }
    account = {}
    # Clock refresh and account snapshots share the execution lock so a
    # revalidation cannot invalidate clock state during a signed order.
    lock = executor.lock if executor else asyncio.Lock()

    def account_is_fresh():
        if not state["authenticated"] or not state["checked_at"]:
            return False
        age = (now() - datetime.fromisoformat(state["checked_at"])).total_seconds()
        return 0 <= age <= 120

    def public_status():
        snapshot = dict(state)
        if snapshot["authenticated"] and not account_is_fresh():
            snapshot.update(status="VALIDATION_STALE", authenticated=False, account_preflight_passed=False, error_code="DEMO_VALIDATION_STALE")
        snapshot["execution_available"] = executor is not None
        snapshot["automatic_enabled"] = bool(executor and executor.ledger.flag("automatic"))
        snapshot["execution_enabled"] = snapshot["automatic_enabled"]
        if executor and snapshot["authenticated"] and executor.ledger.active() and snapshot["status"] == "ACCOUNT_REVIEW_REQUIRED":
            snapshot["status"] = "CONNECTED_DEMO"
        return snapshot

    def mark_failed(code):
        nonlocal account
        account = {}
        state.update(status="CONNECTION_FAILED", authenticated=False, account_preflight_passed=False, market_reachable=False, error_code=code, issues=[])

    async def refresh():
        nonlocal account
        async with lock:
            state["validation_in_progress"] = True
            try:
                if credentials is None:
                    await client.sync_clock()
                    state.update(status="WAITING_FOR_CREDENTIALS", market_reachable=True, error_code=None)
                else:
                    account = await client.validate_account()
                    if executor:
                        executor.last_account={**account,'checked_at':now().isoformat()}
                    state.update(
                        status="CONNECTED_READ_ONLY" if account["account_preflight_passed"] else "ACCOUNT_REVIEW_REQUIRED",
                        market_reachable=True, authenticated=True, error_code=None,
                        account_preflight_passed=account["account_preflight_passed"], issues=account["issues"],
                    )
            except DemoError as exc:
                mark_failed(exc.code)
            except Exception:
                mark_failed("DEMO_VALIDATION_FAILED")
            finally:
                state["validation_in_progress"] = False
            state["checked_at"] = now().isoformat()

    async def monitor():
        while True:
            await refresh()
            await asyncio.sleep(60)

    async def execution_monitor():
        logged_at=0
        while True:
            try:
                await executor.cycle()
            except Exception as exc:
                executor.last_error = exc.code if isinstance(exc, DemoError) else "DEMO_EXECUTION_MONITOR_FAILED"
                executor.ledger.flag("automatic", False)
                executor.ledger.flag("halted", True)
            stamp=asyncio.get_running_loop().time()
            if stamp-logged_at>=60:
                report=executor.observation()
                if report:
                    # Railway's authenticated logs are the supervision source;
                    # no credentials, signed URLs or provider bodies are logged.
                    logging.getLogger('uvicorn.error').info('DEMO_OBSERVATION %s',json.dumps(report,allow_nan=False,separators=(',',':')))
                logged_at=stamp
            await asyncio.sleep(10)

    @asynccontextmanager
    async def lifespan(app):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        task = asyncio.create_task(monitor())
        execution_task = asyncio.create_task(execution_monitor()) if executor else None
        try:
            yield
        finally:
            tasks = [task, *([execution_task] if execution_task else [])]
            for running in tasks:
                running.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
            if executor:
                executor.ledger.close()

    app = FastAPI(title="Capital Cipher — Futuros Demo", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.refresh = refresh
    app.state.demo_client = client
    app.state.demo_executor = executor

    async def require_admin(x_api_key: str | None = Header(default=None)):
        if not x_api_key or not hmac.compare_digest(x_api_key.encode(), admin_key.encode()):
            raise HTTPException(401, "AUTH_REQUIRED")

    @app.middleware("http")
    async def safe_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.get("/health")
    async def health():
        return {"status": "UP", "service": "binance-futures-demo-connector", "execution_enabled": False}

    @app.get("/ready")
    async def ready():
        # This indicates authenticated read readiness only, never order readiness.
        ready = account_is_fresh() and state["account_preflight_passed"]
        return JSONResponse({"ready": ready, "scope": "READ_ONLY_ACCOUNT_PREFLIGHT", "execution_enabled": False}, status_code=200 if ready else 503)

    @app.get("/api/demo/status")
    async def status():
        return public_status()

    @app.get("/api/demo/account", dependencies=[Depends(require_admin)])
    async def get_account():
        if executor:
            try:
                async with executor.lock:
                    return await executor.account()
            except DemoError as exc:
                raise HTTPException(503, exc.code) from None
        if not account or not account_is_fresh():
            raise HTTPException(503, "DEMO_ACCOUNT_NOT_VALIDATED")
        return {**account, "checked_at": state["checked_at"]}

    @app.post("/api/demo/validate", dependencies=[Depends(require_admin)])
    async def validate():
        # Refresh local validation only. The exchange client sends GET requests only.
        if lock.locked():
            raise HTTPException(409, "DEMO_VALIDATION_RUNNING")
        await refresh()
        return public_status()

    @app.get("/api/demo/candles", dependencies=[Depends(require_admin)])
    async def candles(symbol: str = "BTCUSDT", timeframe: str = "15m", limit: int = Query(default=200, ge=1, le=1000)):
        try:
            async with lock:
                rows = await client.candles(symbol=symbol, timeframe=timeframe, limit=limit)
            return {"market": "USD_M_FUTURES_DEMO", "closed_candles_only": True, "execution_enabled": False, "candles": rows}
        except DemoError as exc:
            raise HTTPException(502, exc.code) from None

    def execution():
        if executor is None:
            raise HTTPException(409, "DEMO_EXECUTOR_NOT_CONFIGURED")
        return executor

    async def action(call):
        try:
            return await call()
        except DemoError as exc:
            raise HTTPException(409, exc.code) from None

    @app.get("/api/demo/execution", dependencies=[Depends(require_admin)])
    async def execution_status():
        return execution().summary()

    @app.get("/api/demo/protection-audit/{trade_id}", dependencies=[Depends(require_admin)])
    async def protection_audit(trade_id: str):
        return await action(lambda: execution().protection_audit(trade_id))

    @app.post("/api/demo/analysis", dependencies=[Depends(require_admin)])
    async def analysis(body: AnalysisRequest):
        return await action(lambda: execution().analyze(body.symbol, body.timeframe))

    @app.post("/api/demo/execute", dependencies=[Depends(require_admin)])
    async def execute(body: ExecuteRequest):
        return await action(lambda: execution().execute(body.proposal_id))

    @app.post("/api/demo/close", dependencies=[Depends(require_admin)])
    async def close(body: ExecuteRequest):
        return await action(lambda: execution().close_trade(body.proposal_id))

    @app.post("/api/demo/reconcile", dependencies=[Depends(require_admin)])
    async def reconcile():
        return await action(lambda: execution().reconcile())

    @app.post("/api/demo/kill", dependencies=[Depends(require_admin)])
    async def kill():
        return await action(lambda: execution().kill())

    @app.post("/api/demo/automatic", dependencies=[Depends(require_admin)])
    async def automatic(body: AutomaticRequest):
        return await action(lambda: execution().set_automatic(body.enabled))

    @app.post("/api/demo/reset", dependencies=[Depends(require_admin)])
    async def reset():
        engine=execution()
        result=await action(lambda: engine.reconcile())
        account=result['account']
        if engine.ledger.active() or account['positions'] or account['open_order_count'] or account['open_algo_order_count']:
            raise HTTPException(409, "DEMO_ACCOUNT_NOT_FLAT")
        engine._drawdown_guard(account)
        engine.ledger.flag('halted',False);engine.ledger.flag('automatic',False)
        engine.ledger.event('TECHNICAL_HALT_RESET',{})
        return engine.summary()

    @app.post("/api/demo/canary", dependencies=[Depends(require_admin)])
    async def canary():
        return await action(lambda: execution().canary())

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return PAGE

    return app


class AnalysisRequest(BaseModel):
    symbol: Literal["BTCUSDT", "ETHUSDT", "SOLUSDT"] = "ETHUSDT"
    timeframe: Literal["1m", "5m", "15m", "1h", "4h"] = "15m"


class ExecuteRequest(BaseModel):
    proposal_id: str = Field(min_length=8, max_length=100)


class AutomaticRequest(BaseModel):
    enabled: bool


PAGE = """<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Capital Cipher · Futuros Demo</title><style>
*{box-sizing:border-box}body{margin:0;background:#0c101a;color:#edf1fa;font:16px system-ui,sans-serif;padding:28px;min-height:100vh}main{max-width:700px;margin:8vh auto}.brand{font-size:12px;letter-spacing:3px;color:#8b9bb5}.badge{display:inline-block;background:#2b2510;color:#f6ce67;padding:7px 12px;border-radius:20px;font-size:12px;margin-top:22px}h1{font-size:clamp(30px,6vw,48px);line-height:1.15;margin:20px 0}p{color:#a5b1c8;line-height:1.7}.card{padding:24px;border:1px solid #293349;border-radius:16px;background:#131b2b;margin:24px 0}.row{display:flex;justify-content:space-between;gap:20px;padding:13px 0;border-bottom:1px solid #263146}.row:last-child{border-bottom:0}.value{color:#f6ce67;text-align:right}small{color:#8b9bb5}.note{border-left:3px solid #f6ce67;padding-left:16px}code{font-size:12px;overflow-wrap:anywhere}</style><main><div class="brand">CAPITAL CIPHER</div><div class="badge">BINANCE · FUTUROS DEMO</div><h1>Conexão com a conta de testes</h1><p>Validação de acesso ao mercado e à sua conta de Futuros Demo.</p><div class="card"><div class="row"><span>Conexão</span><span class="value" id="status">Verificando…</span></div><div class="row"><span>Chaves Demo</span><span class="value" id="keys">—</span></div><div class="row"><span>Autenticação</span><span class="value" id="auth">—</span></div><div class="row"><span>Operações automáticas</span><span class="value" id="automatic">Desativadas</span></div></div><p id="instructions">Aguarde a primeira verificação.</p><p class="note">Conexão de Futuros Demo. Análises, posições e controles de execução ficam na área Futuros Demo do dashboard principal do Capital Cipher.</p><small id="checked"></small><p><small>Conta de testes com fundos virtuais.</small></p></main><script>
const labels={CHECKING:'Verificando…',WAITING_FOR_CREDENTIALS:'Aguardando chaves',CONNECTED_READ_ONLY:'Conectada · leitura',CONNECTED_DEMO:'Conectada · Demo',ACCOUNT_REVIEW_REQUIRED:'Revisar conta Demo',CONNECTION_FAILED:'Falha na conexão',VALIDATION_STALE:'Verificação desatualizada'};
async function update(){try{let r=await fetch('/api/demo/status',{cache:'no-store'});if(!r.ok)throw Error();let s=await r.json();document.querySelector('#status').textContent=labels[s.status]||'Verificando';document.querySelector('#keys').textContent=s.credentials_present?'Configuradas':'Pendentes';document.querySelector('#auth').textContent=s.authenticated?'Confirmada':'Pendente';document.querySelector('#automatic').textContent=s.automatic_enabled?'Ativas na Demo':'Desativadas';document.querySelector('#instructions').textContent=s.status==='WAITING_FOR_CREDENTIALS'?'No Railway, abra o serviço capital-cipher-binance-demo e cadastre as duas variáveis de credenciais Demo.':s.authenticated?'Conta autenticada. A liberação de ordens ainda depende das proteções e dos testes de execução.':s.error_code?'Não foi possível validar a conexão. Código: '+s.error_code:'Verificando acesso à Binance Demo…';document.querySelector('#checked').textContent=s.checked_at?'Última verificação: '+new Date(s.checked_at).toLocaleString('pt-BR'):'';}catch{document.querySelector('#status').textContent='Indisponível';}}update();setInterval(update,10000);
</script></html>"""
