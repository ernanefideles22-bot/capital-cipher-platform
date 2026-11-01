# E2.0 — First real-candle chart-skill benchmark (31 October 2026)

**Verdict: NO strategy promoted. No demonstrated profitability or improvement of deployed agents.**

## Reproducible evidence

The fixed reference and fixed chart-reading candidates were defined in
`backend/app/strategy/research_benchmark_e20.py` before the three market
snapshots were fetched. The samples are public BINANCE SPOT OHLCV, 800 complete
candles each, with a fixed cutoff of 2026-10-31T00:00:00Z. Frozen historical
snapshots are under `backend/fixtures/market_samples/`. Their SHA-256s are:

- BTCUSDT 15m: `89b1da9e9796d3ff18b129f7034df88a31d9a323a37770cb42d676db893395b3`
- ETHUSDT 1h: `089128e31df2c7bca17193c4ec2c0dfe13105a377e99cd122c45ac9fdea6a3fc`
- SOLUSDT 4h: `f0b426114e4253b92fc9d5dc2ccf1a77960af5f845fd8ccc1799e6d2ef29c675`

The 40% final bars of every series are held out from rule authoring; rules are
not fitted to the earlier 60% either. The replay requires an as-of completed
and received bar, next eligible open fill, stop-before-target on same bar,
one position per symbol and fixed 0.5% planned equity risk. The hypothetical
receipt timestamp is candle close +1 second. This does NOT establish an
executable quote or actual liquidity.

All percentages below refer to independent, fictional US$10,000 research
trials, NOT the Capital Cipher staging portfolio.

## Observed historical-holdout replay results

| Series | Cost (bps/side) | Reference trades | Reference net return | Chart candidate trades | Chart candidate net return | Candidate max DD |
|---|---:|---:|---:|---:|---:|---:|
| BTCUSDT 15m | 12 | 29 | -7.37396% | 3 | -0.77439% | 0.77439% |
| BTCUSDT 15m | 25 | 29 | -13.76207% | 3 | -1.54534% | 1.54534% |
| ETHUSDT 1h | 12 | 33 | -2.45467% | 5 | -0.76589% | 1.67427% |
| ETHUSDT 1h | 25 | 33 | -6.46383% | 5 | -1.39751% | 2.22017% |
| SOLUSDT 4h | 12 | 29 | -1.77302% | 3 | -0.54332% | 0.58736% |
| SOLUSDT 4h | 25 | 29 | -3.22877% | 3 | -0.68071% | 0.68307% |

The candidates showed less loss but much lower activity (3, 5 and 3 trades).
All candidate returns are negative; none reaches the predeclared 30-trade
minimum, and lower absolute losses from lower exposure are NOT evidence of
superior entry timing. Candidate profit factors at 12 bps/side were zero,
0.54255 and 0.07498 respectively. These are descriptive observations, not
generalizable alpha estimates.

**Crucial:** the ETH 1h research rule is a synthetic SELL/short based on Binance
SPOT candles. It is not an executable Binance SPOT short without inventory.
No Gate.io futures candles or funding/slippage were evaluated. Do not treat
the ETH result as an actionable SPOT setup.

These reference rules are NOT the deployed Capital Cipher agents, and
staging strategy performance was not compared on equivalent execution clocks.
The sample was historical, not a cryptographically sealed external prospective
OOS study. The dataset does not include order-book quotes, intrabar sequence,
real spreads, exchange latency or measured impact.

## Decision and next gates

1. Reject promotion of all three fixed candidates; do not optimize the same
   observed holdout, because doing so would contaminate it.
2. Build a matched, candle-by-candle offline comparison of actual deployed
   agent versions and candidate signals using the same venue, timing, sizing,
   costs and one-way book.
3. Use fresh, previously unseen data and longer horizons for the next research
   trial. Require enough closed trades, robust net expectancy, stress costs,
   drawdown controls and a prospective SHADOW follow-up.
4. Measure abstention, signal calibration and fill feasibility, not merely P&L
   improvement from taking fewer trades.

Neither the OMS, central risk, LIVE, TESTNET nor the Railway operational
backend was changed by this research.
