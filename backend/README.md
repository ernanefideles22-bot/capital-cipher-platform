# Capital Cipher AI — Backend (Phase 1)

FastAPI backend implementing the Capital Cipher AI specification
(`capital-cipher-specification` repo). **Phase 1 operates exclusively in PAPER
mode: no real orders, no private API keys, no live trading code.**

## Requirements

- Python 3.13+ (spec target; code runs on 3.10+)
- pip

## Run locally

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload
```

- `GET /health` — service health
- `GET /api/v1/status` — system mode (PAPER) and component status
- Full API per `docs/13-api-specification.md`

To stream real public market data from Binance (no API key needed):

```bash
ENABLE_MARKET_DATA=1 uvicorn app.main:app
```

## Run tests

```bash
python -m pytest app/tests -q
```

106 tests cover: agent contracts, indicators, risk scenarios (approve/reduce/
block/kill-switch/drawdown/losses/latency/audit-failure), decision engine
(weights, conflicts, minimum confidence), paper trading (SL/TP, fees,
slippage, idempotency), data quality, state machine and Phase 1 security
guarantees (LIVE mode impossible, no API key fields, no real order code).

## Phase 2 additions

- `app/strategy/` — Strategy Engine (docs/26): versioned strategies with risk
  profiles; regime rules (HIGH_VOLATILITY blocks, RANGE raises min confidence).
  Strategy overrides can only tighten docs/06 global limits, never loosen them.
- `app/backtesting/` — Backtesting Engine (docs/17): isolated pipeline replay,
  no lookahead (input order irrelevant), mandatory metrics, fees/slippage.
  API: `POST /api/v1/backtest/run` (source: store | inline | csv).
- Reports: `GET /api/v1/reports/performance?by=symbol|timeframe` (equity curve
  included), `GET /api/v1/reports/agents/ranking` (report-only, docs/27).
- Risk: total-drawdown gate (10% default) + daily reset on UTC day change.

## Architecture

```text
Market Data Adapter (Binance/Bybit/CSV/Replay)
  → Data Quality → CandleStore
  → Orchestrator → [MarketDataAgent, QuantAgent, TrendAgent]
  → Decision Engine (weighted consolidation, no simple voting)
  → Risk Manager (absolute veto, audited before anything advances)
  → Paper Trading Engine (fees + slippage simulated)
  → Audit trail (correlation_id reconstructs every chain)
```

Every state-changing or resource-intensive endpoint (`kill-switch`, manual
evaluation, strategy changes, and backtest execution) requires `X-API-Key`
matching `ADMIN_API_KEY` from the environment. The key must contain at least
32 characters; when unset, those endpoints are locked (fail-safe).

The HTTP boundary also enforces `API_RATE_LIMIT_PER_MINUTE`, rejects request
bodies larger than `MAX_REQUEST_BODY_BYTES`, adds defensive response headers,
and accepts browser origins only from `CORS_ALLOWED_ORIGINS`. The Phase 1
runtime accepts only `SYSTEM_MODE=OFFLINE|PAPER`; testnet and live execution
environments are not represented in configuration or code.

Unauthenticated WebSocket streams are not mounted. They may return only after
the platform has a user-authenticated handshake and per-tenant authorization.
