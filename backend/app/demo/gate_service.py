"""Independent Gate Testnet read-only connection status service."""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.demo.gate_readonly import GateProbeError, probe_account

KEY_VARIABLE = "CAPITAL_CIPHER_GATE_TESTNET_API_KEY"
SECRET_VARIABLE = "CAPITAL_CIPHER_GATE_TESTNET_API_SECRET"


def create_app(environment=None, probe=probe_account):
    values = dict(os.environ if environment is None else environment)
    key, secret = values.get(KEY_VARIABLE, "").strip(), values.get(SECRET_VARIABLE, "").strip()
    state = {"environment": "GATE_TESTNET", "status": "WAITING_FOR_CREDENTIALS",
             "authenticated": False, "execution_enabled": False, "checked_at": None}

    async def refresh():
        state.update(authenticated=False, checked_at=None)
        if not key or not secret:
            state["status"] = "INCOMPLETE_CREDENTIALS" if key or secret else "WAITING_FOR_CREDENTIALS"
            state["checked_at"] = datetime.now(timezone.utc).isoformat()
            return
        try:
            await asyncio.to_thread(probe, key, secret)
        except GateProbeError:
            state["status"] = "CONNECTION_FAILED"
        except Exception:
            state["status"] = "CONNECTION_FAILED"
        else:
            state.update(status="CONNECTED_READ_ONLY", authenticated=True)
        state["checked_at"] = datetime.now(timezone.utc).isoformat()

    async def monitor():
        while True:
            await refresh()
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(monitor())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="Gate Testnet: leitura", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.refresh = refresh

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


PAGE = """<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gate Testnet</title><style>body{background:#0c101a;color:#edf1fa;font:16px system-ui;margin:10vh auto;padding:24px;max-width:600px}section{background:#131b2b;border:1px solid #293349;border-radius:16px;padding:24px}p{line-height:1.6;color:#a5b1c8}strong{color:#f6ce67}</style><h1>Gate · Futuros Testnet</h1><section><p>Conexão: <strong id="status">Verificando...</strong></p><p>Operações automáticas: <strong>Desativadas</strong></p><p>A consulta verifica a autenticação da conta de testes. Nenhuma ordem é enviada.</p></section><script>async function update(){try{const r=await fetch('/api/gate/status',{cache:'no-store'});if(!r.ok)throw 0;const s=await r.json();const l={WAITING_FOR_CREDENTIALS:'Aguardando chaves Testnet',INCOMPLETE_CREDENTIALS:'Chaves incompletas',CONNECTED_READ_ONLY:'Conectada · somente leitura',CONNECTION_FAILED:'Falha na conexão'};document.querySelector('#status').textContent=l[s.status]||'Verificando...'}catch(e){document.querySelector('#status').textContent='Indisponível'}}update();setInterval(update,10000)</script></html>"""

app = create_app()
