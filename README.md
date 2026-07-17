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
