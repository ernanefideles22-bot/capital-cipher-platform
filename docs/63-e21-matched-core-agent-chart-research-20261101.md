# E2.1 — Matched native-core signal overlay vs frozen chart candidates

## Execution and status

Research only, 2026-11-01 UTC; the source samples are the same historical
800-candle public Binance SPOT fixtures already used and reviewed in E2.0.
The GitHub isolated PAPER workflow generated `E21_MATCHED_RESULT` followed
by `E21_MATCHED_COMPLETE`, and its core test suite, E2.0 and E2.1 tests passed.

**Not a new sealed out-of-sample period. Not a prospective performance claim.**
Does not involve all 300 agents, Gate futures market data, real exchange
fills or current operational PAPER P&L.

The source path `backend/scripts/run_e21_matched_core_agent_benchmark.py`
runs the ORIGINAL `BacktestingEngine` containing MarketDataAgent,
QuantAgent, TrendAgent, DecisionEngine, centralized RiskManager and
PaperTradingEngine, plus an optional read-only decision observer. It
records both all pre-risk directional decisions and the decisions approved
by the ORIGINAL native risk path. No production observer was installed.

For comparison, the new research-only shared executor receives (a) native
pre-risk BUY/SELL signals, (b) native approved BUY/SELL subset, and
(c) the E2.0 frozen chart candidate. Each arm gets identical model:
next eligible candle open after signal received, 1 position at a time,
0.5% planned risk, 1.5 ATR stop, 2R target, stop-first intrabar ambiguity,
gap-adverse stop fill, 12/25 bps per side in all-in execution fees, cooldown,
and fictional US$10,000 independent capital.

The native RiskManager's APPROVED subset is path-dependent on the original
native PAPER book; this is **not** a controlled risk-policy comparison.
All baseline and candidate historical samples were already looked at in
E2.0. This is a descriptive diagnostic and cannot support strategy
promotion or tuning on the same historical subset.

## Matched replay observations

| Symbol/timeframe | Costs bps/side | Native all signals | Native net return | Native risk-approved signals | Native approved net return | Chart signals | Chart trades | Chart net return |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTCUSDT 15m | 12 | 59 | -2.98761% | 10 | -2.19128% | 5 | 3 | -0.67472% |
| BTCUSDT 15m | 25 | 59 | -5.43622% | 10 | -3.95250% | 5 | 3 | -1.44644% |
| ETHUSDT 1h | 12 | 58 | -3.42074% | 5 | -2.47919% | 8 | 4 | +0.57170% |
| ETHUSDT 1h | 25 | 58 | -5.28095% | 5 | -3.01240% | 8 | 4 | +0.11735% |
| SOLUSDT 4h | 12 | 63 | -1.85943% | 14 | -1.85943% | 5 | 3 | -1.61724% |
| SOLUSDT 4h | 25 | 63 | -2.23971% | 14 | -2.23971% | 5 | 3 | -1.75224% |

The test window covers 320 bars per market. Native pre-risk signals
entered 11, 16 and 9 trades at 12 bps; the three candidates entered only
3, 4 and 3 trades. A lower loss mainly due to very low exposure is
not evidence of better predictive skill.

**ETH 1h is a synthetic SELL/short research position on Binance SPOT
candles, not a directly executable Binance SPOT short.** The small
positive result is in an unsealed explored sample of four trades. It
must never be used as evidence for Gate futures profitability.

Results differ from E2.0 candidate returns because E2.1
deliberately imposes a SHARED fixed stop/target exit model on every arm
instead of the candidate-specific invalidation exit present in E2.0.

## Accepted outcome and next step

- No existing strategy is changed or promoted. LIVE/TESTNET remain disabled.
- The E2.1 three-core-agent benchmark does not demonstrate that the 300-agent
  runtime itself has improved; it isolates currently versioned core logic.
- Do not tune the candidate using the already inspected October holdout.
- Next: freeze a genuinely future observation window; gather matched
  venue/timeframe data including executable quotes, actual agent identities,
  independent signal outcomes and a realistic OMS execution baseline.
  Evaluate net expectancy, profit factor, signal discrimination, Brier /
  calibration if probability forecasts are emitted, turnover, exposure and
  drawdown with enough trades and adjusted multiple-hypothesis testing.
- No Railway stage or operator action is required for this offline report.
