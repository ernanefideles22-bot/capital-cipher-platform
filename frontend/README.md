# Capital Cipher AI — Dashboard (Phase 1)

React + Vite + TypeScript + Tailwind dashboard per `docs/14-dashboard-specification.md`.

Screens: Overview, Market (Lightweight Charts), Agents, Decisions (with full
audit-chain inspection), Risk, Paper Trading, Audit. Kill Switch always
visible in the header. No real-execution controls exist in Phase 1.

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
