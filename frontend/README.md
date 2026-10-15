# Capital Cipher AI — Dashboard PAPER

React + Vite + TypeScript + Tailwind dashboard per `docs/14-dashboard-specification.md`.

O idioma padrão é português do Brasil, com seletor Português/Inglês persistido
no navegador. O painel institucional mostra a coorte real de agentes, saúde,
cobertura por família, pipeline de governança, mercado, decisões, risco,
paper trading, backtests, relatórios e auditoria. A coorte de 300 agentes é
telemetria real do runtime; a interface não inventa desempenho quando ainda
não existem execuções.

O Kill Switch permanece sempre visível no cabeçalho. Não existem controles de
execução real nesta fase.

## Run

```bash
cd frontend
npm install
npm run dev   # expects backend on http://localhost:8000
```

The dashboard defaults to the same-origin `/api/v1` path. For a separately
hosted static build, set `VITE_API_BASE_URL` to the HTTPS API origin plus
`/api/v1` at build time, and add that exact dashboard origin to the backend's
`CORS_ALLOWED_ORIGINS`. Never put `ADMIN_API_KEY`, exchange credentials, or
database secrets in Vite variables; protected actions prompt for the key and
remain gated by the backend.

The interface is intentionally PAPER-first: it exposes operational telemetry,
research reports, simulated orders, and an authenticated emergency kill switch.
It has no live execution control.
