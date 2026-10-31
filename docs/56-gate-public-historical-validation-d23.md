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
