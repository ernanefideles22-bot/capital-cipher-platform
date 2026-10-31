# Capital Cipher — Real market research smoke test, 2026-10-31

**Experimental report, NOT an investment decision or authorization.**
The data below are **not Gate quotes or actual order fills**.

## Data source and window

Read-only Binance USD-M futures public KLINE API via connected market-data
provider on 2026-10-31. Close cutoff was fixed at **2026-10-31 18:00 UTC**
and the last incompletely closed bar was excluded. No trading API used.

Nine series: BTCUSDT / ETHUSDT / SOLUSDT, each at 15m / 1h / 4h.
Each contained **1499 complete OHLCV bars**.
- 15m: 2026-10-16 through 2026-10-31 (15.6 days)
- 1h: 2026-08-30 through 2026-10-31 (~62.5 days)
- 4h: 2026-02-23 through 2026-10-31 (~250 days)

**Frozen representative source samples in this PR**:
`backend/app/tests/fixtures/research_20261031_btcusdt_15m.json`,
`research_20261031_ethusdt_1h.json`,
`research_20261031_solusdt_4h.json`.

Each frozen file records real exchange OHLCV values, while its
`received_at` is **synthetically assumed** to be
`close_time_ms + 1000ms`; this is not an exchange/network measurement.
Tests in `test_real_market_d2_frozen_20261031.py` execute the
**actual repository D2 Python candidate replay and walk-forward** on
those three fixed datasets (as distinct from the exploratory results below).

## Independent exploratory scan: 54 scenarios

This initial scan executed an **independent JavaScript approximation** of
the D2 candle-entry logic, not a complete execution of the Python D2.2
system. It evaluated six fixed, pre-specified directional heuristics on
each of nine real-OHLCV series:

- Momentum BUY/SELL: EMA20/EMA50 regime and five-bar price momentum;
- EMA20 trend pullback BUY/SELL;
- Prior-20-bar breakout BUY/SELL, with 1.2x relative volume.

**Assumptions**: 60% earliest bars for initial comparison and 40%
chronologically later for OOS; 10,000 USDT isolated starting equity per
scenario, 0.5% risk per trade, 1x cap, ATR14 * 1.5 stop, 2R target,
conservative stop-first OHLCV ambiguity, no same-bar entry, earliest
post-receipt eligible open, and costs of **12 bps per side**.
A separate stress scenario used **25 bps per side**. No funding,
historical bid/ask, mark-price triggering, quote depth, liquidation,
Gate commission tier or multi-strategy position netting was modeled.
The split has been seen during analysis: it is **NOT a sealed holdout**.

| Timeframe | Approximate OOS duration | Positive of 18 (12bps/side) | Positive of 18 (25bps/side) |
|---|---:|---:|---:|
| 15m | 6.25 days | 0 | 0 |
| 1h | 25 days | 7 | 2 |
| 4h | 100 days | 3 | 2 |
| **Total** | Different horizons | **10 / 54** | **4 / 54** |

**Zero of 54** scenarios had positive net returns in **both** the early
training portion and the later OOS portion at 12 bps/side. The four
25-bps OOS-positive candidates ALL had negative returns in the earlier
segment. This is a strong negative preliminary signal **for the specific
rules and tested date ranges**, not proof that all scalping or day trading
is generally unprofitable.

Selected OOS observations (starting each with 10,000 USDT):
- ETHUSDT 1h pullback SELL: +332.38 USDT at 12bps, +208.23 at 25bps;
  training at 12bps **-342.03** (11 OOS trades).
- ETHUSDT 4h momentum BUY: +152.38 USDT at 12bps, +64.36 at 25bps;
  training **-341.63** (14 OOS trades).
- BTCUSDT 4h breakout BUY: +72.72 at 12bps, +44.05 at 25bps;
  training **-204.71** (4 OOS trades).
- SOLUSDT 1h breakout SELL: +156.94 at 12bps, +57.01 at 25bps;
  training **-205.16** (10 OOS trades).

All results are exploratory and highly vulnerable to selection effects:
54 strategies observed, small and unequal trade counts, short 15m
horizon, dependence between overlapping price series, no multiple-testing
approval, and no historical spread/depth. Positive results MUST NOT be
selected for production on the strength of these comparisons.

## Implications and verification

1. Do not enable any new scalper or agent solely based on signal frequency.
2. Verify real Python D2 replay on the frozen Binance candles using the new
   regression tests; distinguish engine functionality from expected profits.
3. Acquire and attest **actual Gate** futures quote/funding and market
   metadata, and replay the **same** windows through the shared D2.2
   quote/one-way portfolio for venue-cost comparisons.
4. Pre-register strategy search budget and fit on training data only.
   Independently reserve a fresh, never-used holdout, and evaluate once
   after selection with multiple-testing and venue-friction controls.
5. Keep risk kill switch, LIVE and Gate writes unchanged.

**No real account orders, Gate Testnet test orders or Railway deployments
were performed for these tests.**
