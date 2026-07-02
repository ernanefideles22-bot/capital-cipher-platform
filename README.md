# Capital Cipher AI — Phase 1 Implementation

Implementation of the [capital-cipher-specification](https://github.com/ernanefideles22-bot/capital-cipher-specification)
Phase 1 MVP + Phase 2 (robust paper trading): multi-agent platform with backtesting engine, strategy engine (SCALP_15M/DAY_1H/SWING_4H), market replay, performance reports per symbol/timeframe, equity curve, total-drawdown control and report-only agent ranking. **No real money, no private
API keys, no live execution — by design and by test.**

```text
backend/   FastAPI + Pydantic + SQLAlchemy (98 tests passing)
frontend/  React + Vite + TS + Tailwind dashboard
docker-compose.yml  backend + PostgreSQL (dev)
```

Quick start: see `backend/README.md` and `frontend/README.md`.
