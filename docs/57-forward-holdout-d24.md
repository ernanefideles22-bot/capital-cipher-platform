# Capital Cipher D2.4 — Future holdout preregistration (prototype)

**Status:** isolated experimental code, stacked on D2.3 PR #65 and D2.2
PR #64. No merge, Railway deploy, OMS mutation, PAPER execution, Gate Testnet
order or LIVE trading is authorized by this module.

## Why freeze before testing?

D2.3 has shown that 5 of 6 fixed Gate OHLCV directional scenarios LOST
net money in the evaluated later 40%. The sole profitable SOL 4h SELL
lost money in the earlier 60%. That already-looked-at period is **not**
an untouched holdout; optimizing parameters on it would introduce
selection bias and could fabricate a profitable-looking history.

D2.4 introduces a forward-only *pre-registration control*. The plan
must be frozen **before the future data become available**.

## Controls implemented

- `ForwardCampaign` requires a 40-character immutable Git commit SHA,
  a 64-character training dataset hash, fixed symbol/timeframe, Gate source,
  frozen `StrategyHypothesis` rules, a candidate budget 1–32, >=2 bar
  train/test embargo by default, and a >=60-bar forward test.
- `ForwardHoldoutRegistry` freezes a canonical payload with SHA-256 and
  HMAC-SHA256, using a separately supplied 256+-bit evaluator secret.
  Its SQLite audit tables reject UPDATE/DELETE and duplicate campaign IDs.
  One-shot evaluation claims the campaign in SQLite before computing
  strategy results. Even a later input/error cannot be retried with the
  same campaign ID. Results are HMAC signed.
- Evaluation rejects attempts before holdout end plus one timeframe interval,
  incorrect bar count, mixed/non-Gate identity, future data, irregular bars,
  and mismatch to the preregistered hypotheses. Uses 60 *preceding*
  bars for indicator warmup, not for scored forward-period trades.
- The existing D2 Python replay is executed twice on the *same* historical
  Gate candles: 12 bps/side and 25 bps/side. No external order calls.
- Every candidate result and every receipt are RESEARCH ONLY and
  `execution_authorized=False`, irrespective of P&L.
- Tests cover premature access, mutated proposals, duplicate invocation,
  insufficient history, wrong venue, tampering, cost stress and immutable
  audit invariants.

## Proposed NEXT future test windows — not yet activated

Proposed initial Gate-specific pre-registration, once an evaluator with
independent signing key and protected historical data exists:

| Campaign | Gate contract | Forward start UTC | Forward end UTC |
|---|---|---|---|
| D24-BTC-SCALP | BTC_USDT 15m | 2026-11-02 00:00 | 2026-11-16 00:00 |
| D24-ETH-DAY | ETH_USDT 1h | 2026-11-02 00:00 | 2026-11-30 00:00 |
| D24-SOL-SWING | SOL_USDT 4h | 2026-11-02 00:00 | 2026-12-31 00:00 |

These are **proposed windows only**. No registry entries were made and no
future observations exist yet. The security plan should be completed before
running a true one-shot evaluation. The historical cutoffs and previous
strategies are already public; freezing needs independently held data and
candidate-identity provenance, not just branch documentation.

## Required isolation — existing prototype is not an independently sealed test

- The D2.4 SQLite registry is **not tamper-proof**: someone with write access
  to its file can drop triggers, replace the file, delete a consumption
  record or register different campaigns. A local HMAC only helps if the
  signing secret is not visible to researchers or trading agents.
- The constructor has an injectable clock for deterministic unit tests.
  In production the evaluator must obtain trusted time externally.
- Agents/research workers must have **no access** to the final holdout
  dataset, evaluator signing key, receipt database or access logs.
- Production storage should use restricted PostgreSQL credentials and
  append-only external audit/checkpoints, with immutable registered
  campaign hashes and a one-shot data-access receipt.
- The evaluator must record data source, actual availability timestamps,
  Git commit and image digest, execution friction model, evaluation window,
  candidate search budget, corrections for all strategy attempts and
  statistically defensible uncertainty bounds.
- As it stands, the forward campaign replay remains **OHLCV only**,
  not authenticated Gate bid/ask/funding/order-book/stop-execution proof.
  We should not convert its sign-flipped return into a trading permit.

## Short-term engineering gates

1. Review and merge D0–D2.4 sequentially only after approval and CI success.
2. Provision a separate trusted evaluator and immutable dataset access.
3. Freeze the hashes/rules BEFORE the future start and collect data without
   giving agents read access. Never create a post-hoc holdout.
4. After the end, run one-shot evaluation and independent signed verification.
5. Only then consider human-reviewed SHADOW, still no direct OMS permission.

**Operational state:** no stop-loss change, no risk kill-switch release, no
testnet canary and no LIVE activation.
