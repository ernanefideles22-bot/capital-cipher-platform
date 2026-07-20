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

The suite covers agent and event contracts, Redis transport, durable outbox,
deterministic replay checkpoints, indicators, risk scenarios, the decision
engine, paper trading, data quality, state transitions, and Phase 1 security
guarantees (LIVE mode impossible, no private exchange keys, no real orders).

Normalized candles are stored idempotently in the internal
`capital_cipher.candle_observations` time-series table before agents, risk, or
paper trading can consume them. Dataset manifests make every backtest input
content-addressable and reproducible. Internal catalog endpoints are protected
by `X-API-Key`; see `../docs/month-3-data-foundation.md`.

Public Binance and Bybit server clocks now gate trusted ingestion. Missing,
stale, or unsafe clock evidence prevents normalized candles from reaching the
warehouse or decision chain. Streaming gaps are persisted automatically, and
administrators can scan or repair bounded historical ranges:

```text
POST /api/v1/market/gaps/scan
GET  /api/v1/market/gaps
POST /api/v1/market/backfills
GET  /api/v1/market/backfills/{job_id}
GET  /api/v1/market/backfills/{job_id}/lineage
```

These endpoints use public market data only and require `X-API-Key`. See
`../docs/month-3-clock-gap-backfill.md` for clock thresholds, idempotency,
provider pagination, and failure semantics. Submission now uses a durable
leased worker queue, archives every raw REST page before normalization, and
exposes request-to-dataset lineage; see
`../docs/month-3-durable-data-lake.md`.

## Event transport and replay

Without `REDIS_URL`, the backend uses only the in-process bus. With a Redis
URL, every event is first journaled in PostgreSQL/SQLite, then published to a
bounded Redis Stream. Failed publications stay in `event_outbox` for retry.

Set `EVENT_BROKER_REQUIRED=1` only when Redis must be healthy before local
PAPER consumers receive events. Replay checkpoints are stored after successful
consumer handling and resume from the next confirmed event. See
`../docs/month-2-redis-replay.md` for delivery and failure semantics.

## Phase 2 additions

- `app/strategy/` — Strategy Engine (docs/26): versioned strategies with risk
  profiles; regime rules (HIGH_VOLATILITY blocks, RANGE raises min confidence).
  Strategy overrides can only tighten docs/06 global limits, never loosen them.
- `app/backtesting/` — Backtesting Engine (docs/17): isolated pipeline replay,
  no lookahead (input order irrelevant), mandatory metrics, and versioned
  adverse entry/exit costs for fees, spread, slippage, volume impact, and
  signed funding.
  APIs: `POST /api/v1/backtest/run` and
  `POST /api/v1/backtest/walk-forward` (source: store | inline | csv).
- Reports: `GET /api/v1/reports/performance?by=symbol|timeframe` (equity curve
  included), `GET /api/v1/reports/agents/ranking` (report-only, docs/27).
- Risk: total-drawdown gate (10% default) + daily reset on UTC day change.

See `../docs/month-4-realistic-execution.md` for formulas, configuration,
report fields, and safety boundaries. See
`../docs/month-4-walk-forward-protocol.md` for pre-registration, embargoed
temporal folds, reproducible experiment identities, and deferred fitting and
promotion controls.
Durable content-addressed reports, atomic idempotency, private PostgreSQL
storage, and mutation guards are documented in
`../docs/month-4-durable-experiments.md`.

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
