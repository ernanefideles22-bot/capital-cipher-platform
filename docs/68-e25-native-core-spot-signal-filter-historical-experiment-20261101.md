# E2.5 — Historical fixed-filter experiment on real, disjoint SPOT candles

## Protocol and data provenance

The fixed E2.5 single-filter specification was committed **before** the public
Binance SPOT OHLCV snapshots were collected. Frozen protocol SHA-256:

`054ce4b8ffed21a6edbbc370f84bb4aa655ea1ef240c96538213e0933be28a80`

- BTCUSDT 15m: 1,000 consecutive bars ending 2026-06-01T00:00:00Z.
- ETHUSDT 1h: 1,000 consecutive bars ending 2026-06-01T00:00:00Z.
- SOLUSDT 4h: 1,000 consecutive bars ending 2026-06-01T00:00:00Z.
- Predeclared evaluation window: bars 300–999, after 300 warmup bars.
- The period is disjoint from E2.0/E2.1 samples, but **historical, not
  prospective**, and not independently sealed by a third-party witness.
- A synthetic receipt latency of +1 second per closed candle is assumed;
  neither executable spreads nor order-book slippage were actually observed.
- Native baseline: versioned **three-core-agent** BacktestingEngine's BUY
  candidate decisions only; all SELLs are excluded symmetrically because this
  is Binance SPOT long-only. This is not a 300-agent runtime simulation.
- Candidate: the same native BUY signals restricted to `UPTREND`, last
  close >= EMA20, last close > close 4 candles prior, relative volume >= 0.8.
- No filter fitting, no strategy search on these data.
- Same isolated E2.1 model on both arms: next eligible opening price,
  1-position one-way book, 0.5% planned trade risk, 1.5ATR stop, 2R target,
  adverse stop-first OHLCV fill, costs of 12/25/40 bps per side.
- Initial research balance per **independent** market: fictional US$10,000.
  These three outcomes are not a correlated portfolio backtest.

## Actual isolated CI replay results

| Market | Cost bps / side | Core BUY signals | Filtered BUY signals | Core trades | Core net return | Filtered trades | Filter net return | Core max DD | Filter max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC 15m | 12 | 41 | 14 | 10 | -4.2357% | 5 | -1.5453% | 4.6830% | 2.0979% |
| BTC 15m | 25 | 41 | 14 | 10 | -6.6305% | 5 | -2.8169% | 6.8247% | 3.1108% |
| BTC 15m | 40 | 41 | 14 | 10 | -9.3199% | 5 | -4.2638% | 9.3199% | 4.2666% |
| ETH 1h | 12 | 43 | 19 | 10 | -3.5503% | 9 | -2.9554% | 3.6246% | 3.0302% |
| ETH 1h | 25 | 43 | 19 | 10 | -5.2560% | 9 | -4.5540% | 5.2560% | 4.5540% |
| ETH 1h | 40 | 43 | 19 | 10 | -7.1899% | 9 | -6.3690% | 7.1899% | 6.3690% |
| SOL 4h | 12 | 56 | 19 | 11 | +1.4835% | 7 | +3.6978% | 1.2274% | 0.5576% |
| SOL 4h | 25 | 56 | 19 | 11 | +0.9404% | 7 | +3.3146% | 1.4679% | 0.6200% |
| SOL 4h | 40 | 56 | 19 | 11 | +0.3170% | 7 | +2.8740% | 1.7449% | 0.6920% |

All native baseline and filtered results use identical cost/execution modeling.
The filtered arm outperformed the native signal arm on *this specific historical*
replay under all nine declared cost-market scenarios. In SOL, 7 trades were
profitable in aggregate, with profit factors 4.26, 3.67 and 3.11 for
12/25/40 bps. These are **sample descriptions**, NOT forward-looking claims.

### Statistical interpretation

- No filtered arm reached even the predeclared **30 closed trades** required
  for initial descriptive evaluation; actual counts are 5, 9, 7.
- Removing signals reduces trading exposure and can lower drawdowns/losses
  without adding predictive edge. There is no matched-trade randomization
  or selection-bias-adjusted inference from these counts.
- BTC and ETH candidate returns remain negative under every cost scenario.
- Only three markets were studied, across incomparable lengths of time and
  correlated assets; results must not be pooled or extrapolated.
- No new parameter tuning on this seen sample. Any further strategy change
  must first pre-register its parameters and reserve different untouched data.
- This was the original 3-core-agent decision engine, not deployed PAPER
  results, all 300 runtime agents, or broker-realistic fill telemetry.
- Review and promotion requirements remain: >=100 proper out-of-sample
  trades, >=3 test folds, stressed costs, corrected statistical inference,
  venue validation, and subsequent independent prospective SHADOW data.
- **No LIVE, TESTNET, automated promotion, risk modification, OMS/backend
  deployment or account changes occurred.**

## Next decision

Do not deploy the filter. Prepare a prospective, externally anchored
future-candle comparison to measure whether it still improves net expectancy
and opportunity-adjusted performance as new PAPER/SHADOW observations arrive.
Also validate actual agent-specific contributions; three-core-engine results
cannot be used as proof for the other 297+ agents.
