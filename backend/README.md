# Capital Cipher AI — Backend

FastAPI backend implementing the Capital Cipher AI specification. PAPER is the
safe default. A gated OMS may use Binance or Bybit TESTNET with runtime-only
sandbox credentials. **LIVE execution and live exchange hosts do not exist.**

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
engine, PAPER trading, TESTNET OMS, reconciliation, data quality, state
transitions, specialist evidence, forecast evaluation, and security guarantees
that keep LIVE execution impossible.

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
temporal folds and reproducible experiment identities.
Durable content-addressed reports, atomic idempotency, private PostgreSQL
storage, and mutation guards are documented in
`../docs/month-4-durable-experiments.md`.
The completed Month 4 boundary—historical spread/funding data, margin and
liquidation, train-only fitting, statistical gates, and versioned Supabase
migration—is documented in `../docs/month-4-completion.md`.

## Governed agent runtime

The governed runtime now hosts exactly 200 analytical PAPER agents:
three existing primary decision agents and 197 evidence-only shadow
specialists. Every execution has a versioned contract, deterministic
idempotency identity, bounded retries, a recoverable lease, isolated
append-only memory, and a complete trace.

Durable execution APIs require `X-API-Key`:

```text
POST /api/v1/agents/executions
GET  /api/v1/agents/executions
GET  /api/v1/agents/executions/{execution_id}
```

The 197 shadow agents have no direct decision, risk or order authority. Month 9
may consume eligible out-of-sample outputs through a bounded confirmation
service that can only preserve a primary candidate or tighten it to `WAIT`. See
`../docs/month-5-agent-runtime.md` for the cohort, contracts, recovery
semantics, storage migration, and exit evidence.

Month 6 adds the central portfolio-risk authority, gross/net/symbol/strategy
exposure, concentration, historical VaR with a conservative fallback,
single-use execution approvals and a durable kill switch. See
`../docs/month-6-central-risk-engine.md` for limits, transaction semantics,
the then-current 40-agent cohort and completion evidence.

Month 7 adds the single order-management boundary, atomic PAPER mirrors,
durable TESTNET commands, exact Binance Spot/Bybit linear sandbox allowlists,
immutable fills and continuous venue reconciliation. Critical drift activates
the central durable kill switch. See
`../docs/month-7-oms-testnet-reconciliation.md` for configuration, API,
transaction semantics, migration and completion evidence.

Month 8 adds 60 narrowly scoped shadow specialists: 20 technical, 15
derivatives, 10 macro, 10 on-chain and 5 news agents. External specialists
consume only normalized evidence with source identity, checksum, quality and
freshness; missing or invalid evidence produces `WAIT` with zero confidence.
Every output is captured as an immutable one-candle forecast and later scored
for directional accuracy, Brier loss and leave-one-out marginal contribution.
Scorecards remain observational until Month 9. See
`../docs/month-8-specialist-cohort-evaluation.md`.

Authenticated Month 8 APIs:

```text
POST /api/v1/agents/evidence
GET  /api/v1/agents/evidence
GET  /api/v1/agents/evaluation/forecasts
GET  /api/v1/agents/evaluation/scorecards
```

Month 9 adds 50 deterministic OHLCV diagnostic agents for an exact cohort of
150 PAPER agents (3 primary, 147 shadow), versioned consensus experiments,
100-sample performance eligibility, concentration-capped weights, rolling
agent-version drift and bounded portfolio targets. Consensus can only confirm
or veto a primary direction, while portfolio construction can only tighten
central-risk notional. See
`../docs/month-9-portfolio-consensus-drift.md`.

Authenticated Month 9 APIs:

```text
POST /api/v1/governance/experiments
POST /api/v1/governance/experiments/{experiment_id}/events
GET  /api/v1/governance/experiments
GET  /api/v1/governance/consensus
GET  /api/v1/governance/drift
GET  /api/v1/governance/portfolio-proposals
```

Month 10 adds 50 deterministic OHLCV resilience diagnostics and an operational
control plane for the exact 200-agent PAPER cohort. Bounded metrics, explicit
SLO/error-budget evaluations, append-only alert lifecycles, cost attribution,
and dependency recovery gates are observational controls. A critical
database, audit or risk failure halts decision evaluation. An optional
dependency failure or daily cost hard limit suspends shadow work while always
preserving the three primary agents and central risk.

Authenticated Month 10 APIs:

```text
GET  /api/v1/operations/status
GET  /api/v1/operations/metrics
POST /api/v1/operations/slos/evaluate
GET  /api/v1/operations/slos
GET  /api/v1/operations/alerts
GET  /api/v1/operations/costs
GET  /api/v1/operations/resilience-runs
```

There is deliberately no chaos-injection HTTP endpoint. Run the isolated,
credential-free acceptance harness locally with:

```bash
python scripts/run_month10_resilience.py
```

See `../docs/month-10-resilience-observability.md`.

## Architecture

```text
Market Data Adapter (Binance/Bybit/CSV/Replay)
  → Data Quality → CandleStore
  → Orchestrator → Agent Runtime (3 PRIMARY + 197 SHADOW)
  → Operational Gate (SLOs + cost + dependency recovery)
  → Observational Evaluation (forecast + outcome + scorecard)
  → Decision Engine (weighted consolidation, no simple voting)
  → Performance Consensus (shadow/default; confirmation only tightens)
  → Portfolio Construction (advisory notional ceiling)
  → Central Risk (portfolio VaR + single-use approval + absolute veto)
  → OMS (atomic PAPER mirror or durable TESTNET command)
  → Exchange reconciliation (fills + positions + balances)
  → Audit trail (correlation_id reconstructs every chain)
```

Every state-changing or resource-intensive endpoint (`kill-switch`, manual
evaluation, strategy changes, and backtest execution) requires `X-API-Key`
matching `ADMIN_API_KEY` from the environment. The key must contain at least
32 characters; when unset, those endpoints are locked (fail-safe).

The HTTP boundary also enforces `API_RATE_LIMIT_PER_MINUTE`, rejects request
bodies larger than `MAX_REQUEST_BODY_BYTES`, adds defensive response headers,
and accepts browser origins only from `CORS_ALLOWED_ORIGINS`. `SYSTEM_MODE`
still accepts only `OFFLINE|PAPER`. The separate OMS boundary accepts PAPER or
an explicitly acknowledged TESTNET; LIVE is not represented in configuration,
schemas, adapters or routes.

Unauthenticated WebSocket streams are not mounted. They may return only after
the platform has a user-authenticated handshake and per-tenant authorization.
