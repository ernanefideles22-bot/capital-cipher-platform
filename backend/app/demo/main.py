"""Standalone Futures Demo connector. No OMS, database or order writer is loaded."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query
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
    if values.get("DEMO_EXECUTION_ENABLED", "false").lower() != "false":
        raise RuntimeError("DEMO_EXECUTION_NOT_IMPLEMENTED")
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
    client = BinanceFuturesDemoClient(credentials, transport=transport, clock_ms=clock_ms)
    state = {
        "status": "CHECKING", "market": "USD_M_FUTURES_DEMO", "credentials_present": credentials is not None,
        "authenticated": False, "market_reachable": False, "account_preflight_passed": False,
        "execution_enabled": False, "live_execution_available": False,
        "checked_at": None, "error_code": None, "issues": [], "validation_in_progress": False,
        "source_revision": values.get("APP_SOURCE_REVISION", "unknown"),
    }
    account = {}
    lock = asyncio.Lock()

    def account_is_fresh():
        if not state["authenticated"] or not state["checked_at"]:
            return False
        age = (now() - datetime.fromisoformat(state["checked_at"])).total_seconds()
        return 0 <= age <= 120

    def public_status():
        snapshot = dict(state)
        if snapshot["authenticated"] and not account_is_fresh():
            snapshot.update(status="VALIDATION_STALE", authenticated=False, account_preflight_passed=False, error_code="DEMO_VALIDATION_STALE")
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

    @asynccontextmanager
    async def lifespan(app):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        task = asyncio.create_task(monitor())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()

    app = FastAPI(title="Capital Cipher — Futuros Demo", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.refresh = refresh
    app.state.demo_client = client

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

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return PAGE

    return app


PAGE = """<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Capital Cipher · Futuros Demo</title><style>
*{box-sizing:border-box}body{margin:0;background:#0c101a;color:#edf1fa;font:16px system-ui,sans-serif;padding:28px;min-height:100vh}main{max-width:700px;margin:8vh auto}.brand{font-size:12px;letter-spacing:3px;color:#8b9bb5}.badge{display:inline-block;background:#2b2510;color:#f6ce67;padding:7px 12px;border-radius:20px;font-size:12px;margin-top:22px}h1{font-size:clamp(30px,6vw,48px);line-height:1.15;margin:20px 0}p{color:#a5b1c8;line-height:1.7}.card{padding:24px;border:1px solid #293349;border-radius:16px;background:#131b2b;margin:24px 0}.row{display:flex;justify-content:space-between;gap:20px;padding:13px 0;border-bottom:1px solid #263146}.row:last-child{border-bottom:0}.value{color:#f6ce67;text-align:right}small{color:#8b9bb5}.note{border-left:3px solid #f6ce67;padding-left:16px}code{font-size:12px;overflow-wrap:anywhere}</style><main><div class="brand">CAPITAL CIPHER</div><div class="badge">BINANCE · FUTUROS DEMO</div><h1>Conexão com a conta de testes</h1><p>Validação de acesso ao mercado e à sua conta de Futuros Demo.</p><div class="card"><div class="row"><span>Conexão</span><span class="value" id="status">Verificando…</span></div><div class="row"><span>Chaves Demo</span><span class="value" id="keys">—</span></div><div class="row"><span>Autenticação</span><span class="value" id="auth">—</span></div><div class="row"><span>Operações automáticas</span><span class="value">Desativadas</span></div></div><p id="instructions">Aguarde a primeira verificação.</p><p class="note">Esta etapa valida a conexão. Os agentes ainda não enviam ordens para a Binance Demo. A execução depende da integração das proteções e da validação do ciclo de ordens.</p><small id="checked"></small><p><small>Conta de testes com fundos virtuais.</small></p></main><script>
const labels={CHECKING:'Verificando…',WAITING_FOR_CREDENTIALS:'Aguardando chaves',CONNECTED_READ_ONLY:'Conectada · leitura',ACCOUNT_REVIEW_REQUIRED:'Revisar conta Demo',CONNECTION_FAILED:'Falha na conexão',VALIDATION_STALE:'Verificação desatualizada'};
async function update(){try{let r=await fetch('/api/demo/status',{cache:'no-store'});if(!r.ok)throw Error();let s=await r.json();document.querySelector('#status').textContent=labels[s.status]||'Verificando';document.querySelector('#keys').textContent=s.credentials_present?'Configuradas':'Pendentes';document.querySelector('#auth').textContent=s.authenticated?'Confirmada':'Pendente';document.querySelector('#instructions').textContent=s.status==='WAITING_FOR_CREDENTIALS'?'No Railway, abra o serviço capital-cipher-binance-demo e cadastre as duas variáveis de credenciais Demo.':s.authenticated?'Conta autenticada. A liberação de ordens ainda depende das proteções e dos testes de execução.':s.error_code?'Não foi possível validar a conexão. Código: '+s.error_code:'Verificando acesso à Binance Demo…';document.querySelector('#checked').textContent=s.checked_at?'Última verificação: '+new Date(s.checked_at).toLocaleString('pt-BR'):'';}catch{document.querySelector('#status').textContent='Indisponível';}}update();setInterval(update,10000);
</script></html>"""
