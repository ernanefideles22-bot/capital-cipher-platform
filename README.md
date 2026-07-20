# Capital Cipher AI — PAPER and gated TESTNET platform

Active implementation of the
[capital-cipher-specification](https://github.com/ernanefideles22-bot/capital-cipher-specification):
an institutional multi-agent research platform with realistic backtesting,
central portfolio risk, an idempotent order management system, PAPER trading,
and explicitly gated exchange TESTNET adapters.

**LIVE execution is absent by design and by test.** The default remains PAPER.
TESTNET requires PostgreSQL, the Month 7 migration, an explicit enable flag,
an exact acknowledgement, runtime-only sandbox credentials, and an exact
testnet host allowlist.

```text
backend/             FastAPI + Pydantic + SQLAlchemy
frontend/            React + Vite + TypeScript dashboard
packages/contracts/  versioned, language-neutral JSON Schema contracts
Redis Streams        optional durable broker with PostgreSQL outbox
Data warehouse       time-series, manifests, clock gates, gaps, backfills
Agent runtime        registry, durable queue, isolated memory, 40 PAPER agents
Central risk         portfolio limits, VaR, single-use approvals, kill switch
OMS                  PAPER mirror, Binance Spot/Bybit linear TESTNET outbox
turbo.json           monorepo task graph
docker-compose.yml   backend + PostgreSQL (development)
```

Install the JavaScript workspace with `pnpm install`, install the backend with
`python -m pip install -e "backend[dev]"`, then run the complete quality gate
with `pnpm check`. See `backend/README.md` and `frontend/README.md` for
service-specific commands.

Repository roles are described in [MIGRATION.md](MIGRATION.md). Incremental
architecture and completion evidence:

- [Month 2 event foundation](docs/month-2-event-foundation.md)
- [Month 2 Redis replay](docs/month-2-redis-replay.md)
- [Month 3 data foundation](docs/month-3-data-foundation.md)
- [Month 3 clocks, gaps and backfills](docs/month-3-clock-gap-backfill.md)
- [Month 3 durable data lake](docs/month-3-durable-data-lake.md)
- [Month 4 realistic execution](docs/month-4-realistic-execution.md)
- [Month 4 walk-forward protocol](docs/month-4-walk-forward-protocol.md)
- [Month 4 durable experiments](docs/month-4-durable-experiments.md)
- [Month 4 completion](docs/month-4-completion.md)
- [Month 5 governed agent runtime](docs/month-5-agent-runtime.md)
- [Month 6 central risk engine](docs/month-6-central-risk-engine.md)
- [Month 7 OMS, TESTNET and reconciliation](docs/month-7-oms-testnet-reconciliation.md)

The specification repository remains the authoritative product and
architecture specification. This repository is the executable platform.
