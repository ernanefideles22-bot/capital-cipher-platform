# E2.6 — Pre-registered future SPOT long-only skill study

## What is actually implemented

The E2.5 deterministic trend filter is fixed and *not retrained* on future
observations. Before the future observation period, E2.6 commits:

- A SHA-256-addressable campaign protocol, pinned to the E2.5 strategy
  protocol digest, with a fixed three-market, three-timeframe universe.
- A standalone public-Binance-SPOT, read-only, **one-cycle-at-a-time**
  observation collector. It uses only public `/api/v3/klines`, does not
  load any private API keys, and cannot place orders.
- Exact 300-candle historical warmup per market and automatic, subsequent
  *single new closed candle* append logic; duplicate events and lost intervals
  fail closed instead of being invented from historical backfills.
- An offline replay of identical native three-core-agent BUY signals, with
  and without the E2.5 fixed trend/EMA20/momentum/relative-volume gate.
- Identical positions, R-multiple exit logic, and stressed simulated execution
  costs of 12/25/40 bps per side for both comparison arms.
- Explicit rejection of nonexistent, stale, fabricated, unobserved or
  incomplete forward candles and all premature performance requests.

**This study does not claim to have started a continuous collector.**
The collector is a *program*, not a running task. No job scheduling,
persistent hosted volume, independent timestamp signature, or source
attestation is installed by this PR. Neither future observation files nor
live prospective validation results exist as of implementation.

## Frozen future study dates (UTC)

| Item | Frozen definition |
|---|---|
| First evaluation candle closes | 2026-11-05 00:00 UTC |
| Final evaluation cutoff, exclusive | 2026-12-05 00:00 UTC |
| Warmup per market | 300 candles |
| BTCUSDT 15m | 2,880 forward candles |
| ETHUSDT 1h | 720 forward candles |
| SOLUSDT 4h | 180 forward candles |
| Maximum self-recorded observation lag | 1 entire timeframe |
| Positions | BINANCE SPOT long-only, no synthetic short |
| Automatic strategy promotion | DENIED |
| LIVE / TESTNET | DISABLED |

Timeframes span different market periods in the warmup, so results may not be
pooled as a diversified portfolio. The data are future *relative to the rule
registration* but self-recorded observation timestamps alone do not constitute
independent cryptographic proof of prospective market access.

## Observational semantics

`backend/scripts/capture_e26_public_spot_observations.py` writes
`e26_SYMBOL_TIMEFRAME_observed.csv` in a **durable operator-owned folder**.
The UTC receipt timestamp is read *after* the REST response. The collector
must begin within one candle duration after the first campaign bar closes,
then be invoked at a cadence sufficiently frequent to observe every new
15-minute candle. It refuses to reconstruct missed evaluation bars.

To **configure** a hosted collector, a separate approval and deployment are
necessary: a persistent write location, operational run schedule, logging,
clock synchronization and trusted external evidence anchor. The GitHub
workflow currently tests only fake public-provider responses; CI is NOT
scheduled as a forward collector. Public Binance REST may be blocked or
rate-limited from some hosting regions. Do not infer operational readiness
from passing unit tests.

Code entry points (not commands executed during this research stage):

```bash
# On an approved, persistent, separate read-only research runner:
python -m scripts.capture_e26_public_spot_observations --directory /durable/e26

# Only after the future window has completely elapsed, from same directory:
python -m scripts.run_e26_future_gate_evaluation --data-dir /durable/e26
```

If the first collection is late or any candle is missed, **do not silently
backfill and label that campaign prospective**. Record the failure and
preregister another future campaign instead.

## Boundaries and interpretation

- E2.6 validates **native three-core-agent** decision logic against a fixed
  selectivity filter; it does *not* individually evaluate the 300 runtime
  agents. A separate runtime-attribution campaign needs immutable agent
  versions and real as-of decision/output logs.
- A valid data file is still only a declared source; actual exchanges, spreads,
  clock accuracy and append-only immutability require independent verification.
- First 30 trades per arm are a descriptive minimum, not statistical proof.
  Research review requires >=100 trades, at least 3 independent test folds,
  adjusted multiple-testing, profitable stressed-cost expectancy, drawdown and
  full execution model validation. A positive month by itself is insufficient.
- Research-only comparisons have **zero trading authority**. No OMS, risk
  engine, deployed agents, broker API keys, LIVE/TESTNET, Railway backend
  service, original PostgreSQL or volumes are changed.
- Until real, durable, independently timestamped data exist, the correct
  campaign status is **FROZEN_WAITING_FOR_INDEPENDENT_FUTURE_CANDLE_COLLECTION**.
