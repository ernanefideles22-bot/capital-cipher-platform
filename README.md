# Capital Cipher AI — active PAPER platform

Implementation of the
[capital-cipher-specification](https://github.com/ernanefideles22-bot/capital-cipher-specification):
a multi-agent research and paper-trading platform with backtesting, strategy
selection, market replay, performance reports, drawdown controls, and
report-only agent ranking. **No real money, no private exchange keys, and no
live execution — by design and by test.**

```text
backend/             FastAPI + Pydantic + SQLAlchemy
frontend/            React + Vite + TypeScript dashboard
packages/contracts/  versioned, language-neutral JSON Schema contracts
Redis Streams        optional durable broker with PostgreSQL outbox
Data warehouse       time-series, manifests, clock gates, gaps, backfills
turbo.json           monorepo task graph
docker-compose.yml   backend + PostgreSQL (development)
```

Install the JavaScript workspace with `pnpm install`, install the backend with
`python -m pip install -e "backend[dev]"`, then run the complete quality gate
with `pnpm check`. See `backend/README.md` and `frontend/README.md` for
service-specific commands.

This repository is the active PAPER implementation. The
[`capital-cipher-specification`](https://github.com/ernanefideles22-bot/capital-cipher-specification)
repository remains the authoritative product and architecture specification.
See [MIGRATION.md](MIGRATION.md) for repository roles and
[`docs/month-2-event-foundation.md`](docs/month-2-event-foundation.md) for the
current contracts and data-ingestion foundation.

The Redis transport and replay guarantees are documented in
[`docs/month-2-redis-replay.md`](docs/month-2-redis-replay.md).
The first Month 3 data foundation is documented in
[`docs/month-3-data-foundation.md`](docs/month-3-data-foundation.md).
Trusted Binance/Bybit clocks, continuity scans, and public historical
backfills are documented in
[`docs/month-3-clock-gap-backfill.md`](docs/month-3-clock-gap-backfill.md).
Durable worker leases, retries, dead letters, raw provider storage, and full
backfill lineage are documented in
[`docs/month-3-durable-data-lake.md`](docs/month-3-durable-data-lake.md).
The first Month 4 increment—versioned spread, slippage, volume impact, fees,
and funding assumptions—is documented in
[`docs/month-4-realistic-execution.md`](docs/month-4-realistic-execution.md).
Leakage-resistant, pre-registered walk-forward folds are documented in
[`docs/month-4-walk-forward-protocol.md`](docs/month-4-walk-forward-protocol.md).
Durable, append-only walk-forward artifacts are documented in
[`docs/month-4-durable-experiments.md`](docs/month-4-durable-experiments.md).
