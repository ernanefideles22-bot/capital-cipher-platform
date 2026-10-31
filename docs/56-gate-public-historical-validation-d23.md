# Capital Cipher — D2.3: read-only Gate historical evidence

**Status:** research-only GitHub PR. No Railway deployment, paid infra,
API key, testnet order, LIVE authorization, OMS or risk-control change.

## Why this phase

D0–D2.2 passed local/synthetic tests and a limited public Binance futures
OHLCV smoke. The strategy outcomes are predominantly negative. We need
**venue-specific public history** before any Gate-related strategy conclusion.

Gate's authenticated exchange identity was absent from the main research
branch, which previously enumerated only BINANCE and BYBIT. This branch
adds `Exchange.GATE` to avoid mislabeled market data.

## Implementation

`backend/scripts/gate_public_history_research.py`:
- Host allowlisted to `https://api.gateio.ws/api/v4`, HTTP **GET** only
- Routes allowlisted to `/futures/usdt/candlesticks` and
  `/futures/usdt/funding_rate` (never private order endpoints)
- Explicit markets BTC_USDT 15m, ETH_USDT 1h, SOL_USDT 4h
- Frozen cutoff: **2026-10-31 18:00:00 UTC**
- Requests `from`/`to` (NOT `limit`, which conflicts with those parameters)
- Rejects incomplete/duplicate/misaligned/non-consecutive/non-finite OHLCV
- Requires >=600 fully closed bars per series
- Maintains `exchange=GATE` data identity
- Real historical Kline API does **not** report the original network receipt
  timestamp. The replay explicitly **assumes close + 1 second**, an
  approximation not an observed source timestamp.
- Fetches historical funding and preserves its raw values and settlements.
  Funding is **not incorporated in the candle-only P&L** because the API
  does not prove when each retrospective rate became knowable; no backfill
  of allegedly observed quotes occurs.
- Creates source-labeled immutable digest manifests and per-pair artifacts
  in an isolated CI runner.
- Replays fixed BUY/SELL directional momentum on the actual project's
  Python D2 candidate engine: first 60% training comparison, last 40%
  chronological evaluation, 12bps and 25bps/side execution stresses,
  1x simulation. Results remain `RESEARCH_ONLY`.
- CI workflow `.github/workflows/gate-public-history-research.yml`
  runs only when this PR modifies the research workflow/script/tests;
  it has `contents:read`, requires no secrets, and exports any available
  outputs to a short-lived GitHub Actions artifact. The external API can
  return 403/network failure; that is an **unavailable data** outcome, not
  financial validation.

Run locally with network and backend dependencies:
```bash
PYTHONPATH=backend python backend/scripts/gate_public_history_research.py \
  --cutoff 2026-10-31T18:00:00+00:00 \
  --out /tmp/gate-research-20261031
```
Unit tests are offline and use synthetic API-shaped records, never claim
those test fixtures came from Gate.

## Hard boundaries

1. These are genuine Gate OHLCV candles **only if the public API call
   actually succeeds and the source archive is present**. A successful
   code/CI test by itself is not evidence data were downloaded.
2. Historical candle OHLCV is NOT contemporaneous best bid/ask, order-book
   depth, actual impact, exchange stop triggering, or realized Gate fills.
   D2.2 quote/funding one-way venue execution must fail closed rather than
   create fabricated historical quotes.
3. Funding timestamps are settlement timestamps; exact retrospective
   availability was not independently recorded.
4. Because the historical cutoff and other 54 scenario results were already
   inspected, this fixed sample is NOT a sealed holdout. A fresh later
   period or separately held, pre-registered, inaccessible dataset is
   required before promotion.
5. No new agent strategy is profitable by default. No self-promotion.
6. No GitHub workflow performs a deployment or sends orders.

## Developer references

- Gate futures Klines:
  https://www.gate.com/docs/developers/apiv4/en/futures/#futures-market-k-line-chart
- Gate futures funding history:
  https://www.gate.com/docs/developers/apiv4/en/futures/#futures-market-historical-funding-rate
- Gate public order book:
  https://www.gate.com/docs/developers/apiv4/en/futures/#query-futures-market-depth-information


## Observed Gate public REST research run — 2026-10-31

**Verified execution:** GitHub Actions run #2, run ID `41236713068`,
job completed **SUCCESS**. Seven source-normalization unit tests passed.
GitHub downloaded *actual public Gate futures OHLCV* and historical funding
records, and ran the genuine `app.strategy.candidate_replay.replay_candidate`
Python module on three markets. The data artifact is available for **7 days**:

- https://github.com/ernanefideles22-bot/capital-cipher-platform/actions/runs/41236713068/artifacts/13055987102

Frozen cutoff: 2026-10-31 18:00:00 UTC. Each symbol: **901 closed candles**
(2703 in total). This is a new historical source, but NOT a fresh sealed
out-of-sample period; the team has already inspected this cutoff.

| Market | Bars | Funding records returned | Later sample BUY (USDT) | Later sample SELL (USDT) |
|---|---:|---:|---:|---:|
| BTC_USDT 15m | 901 | 28 | -141.56921790 | -132.02636694 |
| ETH_USDT 1h | 901 | 100 | -92.51408635 | -92.40423889 |
| SOL_USDT 4h | 901 | 100 | -190.84996165 | +121.33880003 |

All results assume separate 10,000-USDT research accounts per scenario,
12 basis points all-in per side, simulated ATR stop and target and next
eligible open. The single later-positive SOL 4h SELL had **-193.43540811**
in the earlier 60% and **+70.35863434** later when stressed to 25bps/side.
No candidate shows consistent profitability across both samples.

Actual Gate raw Kline record hashes recorded by the collector:
- BTC 15m: `76b1fa51bfa349b77d81575a3dc630cae4db76b465ff9a66017f2934fbedb798`
- ETH 1h: `71f9d18a00bcfeb46c29190a71bca271e25ecb458cdf96a5d51818e3ebc8fd83`
- SOL 4h: `2c7aaea6e4d042b91d5d0f970b0d0dc536846190e04a100eb80c86842d8a149d`

**Data-quality limitations:** The funding endpoint returned 100 records
for ETH and SOL, so completeness across their long candle windows has **not**
been proven. Funding was saved but **NOT used in PnL**. Real historical Gate
bid/ask, queue depth and order fills remain unavailable; the 12/25bps
execution assumptions are stress heuristics, not empirically calibrated
Gate execution costs.

There were **zero new trade orders** and no Railway changes in this run.
