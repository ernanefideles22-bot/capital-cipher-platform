# Capital Cipher — Gate Professional Trading Stage 2 (research foundation)

**Status: RESEARCH / NOT DEPLOYED / NOT PERFORMANCE-VALIDATED.**

Base: real Gate service commit `2eb009ba161ce42005de462e343901de5ae2abfd`; the Stage 1 24-hour operational validation continues separately. This Stage 2 branch does not alter any Railway service, deployed Gate code, mounted volume, historical agent signals, broker configuration, OMS, or central risk manager.

## Scoped Stage 2A deliverable

A deterministic independent prototype with three classical, versioned research hypotheses:

| ID | Entry requirement | Proposed bracket |
| --- | --- | --- |
| `EMA_TREND_PULLBACK_V1` | EMA20/EMA50 trend, closed-candle reclaim/rejection of EMA20, RSI guard | 1.5 ATR stop, 2R objective, 24-bar limit |
| `DONCHIAN_VOLUME_BREAKOUT_V1` | New close beyond prior 20-bar high/low, above-baseline volume, matching EMA trend | 2 ATR stop, 2R objective, 24-bar limit |
| `RANGE_BOLLINGER_REVERSION_V1` | EMA separation consistent with range, close beyond 1.7 population σ, RSI guard | 1.5 ATR stop, 1.5R objective, 16-bar limit |

All thresholds are *unproven hypotheses*. A named candlestick or familiar indicator does **not** imply profitable trading. Each `ResearchPlan` explicitly rejects execution permission and predictive-edge claims. No automatic agent registration, risk promotion, BUY/SELL output to the existing OMS, or broker commands exist in these modules.

## Explicit replay assumptions and controls

1. Read historical Gate Futures Testnet OHLCV in 15m, 1h or 4h for BTC, ETH, SOL. The replay uses simulated hypothetical bar-close availability, **NOT** historical network-receipt evidence. It uses only data through signal bar `i`, then fills at open of `i+1`. No same-bar-close entries.
2. A single 1x-capped hypothetical position per research series. Risk budget max 0.25% hypothetical equity per trade; notional cap 100% equity, daily realized-equity loss gate 2%, portfolio drawdown gate 10%. These are simulation limits only; they do NOT supersede the actual Gate central risk manager.
3. Bracket stop and objective anchored to next-open fill. Stop first on ambiguous dual-touch bars. If an open gaps past a stop, assume that adverse open, not the wished-for stop price. Time-limit expiry exits at a later bar's open.
4. Disclose 6 bps exchange fee + 6 bps slippage per side (12 bps per side combined), and conservatively estimate 1 bp funding every 8 hours (minimum one funding interval per closed simulated trade). These are user-defined stress assumptions, **not** observed venue execution costs. Evaluate net after all charges.
5. No overlapping trades, no position carried across train/OOS boundary. Ending positions have `END_WINDOW_ESTIMATE` and must be excluded from completed-trade credibility counts.
6. Split chronological data first 60% TRAIN, final 40% OOS. Select the best candidate on TRAIN only if it has at least 5 closed training trades, net profit >0 after stress costs and measurable PF>1. Run exactly the frozen winner once on OOS; never tune using OOS. No-selection is a valid, expected outcome.
7. OOS reports are explicitly exploratory. At least 30 *completed* OOS trades are necessary merely for a preliminary sample, not statistical proof or release. A 200-bar public snapshot is inadequate to validate consistent profitability. Confidence intervals, selection bias/multiple-testing correction, several walk-forward folds, across-regime time ranges and Gate actual costs remain outstanding.
8. No trade is sent by this code. Neither automatic promotion nor capital deployment is authorized even if training or OOS shows positive PnL.

## Test gates

`backend/app/tests/test_gate_s2_classical_replay.py` tests closed-candle causality, future-receipt rejection, directional breakout, next-bar entry, simultaneous stop/target resolution, adverse gap, fees/slippage/funding, daily loss gate, chronological frozen selection and no downstream order authority.

`backend/scripts/gate_s2_live_research.py` performs a **one-shot read-only** Gate Futures Testnet diagnostic across nine 200-bar series and reports TRAIN and OOS observations. It is separate from Railway and uses no secrets, OMS or exchange trading API.

## Outstanding for full Stage 2

- Real historical sample with sufficient independent closed trades, reliable actual Gate fee/slippage/funding assumptions and several volatility/trend regimes.
- Rolling/purged walk-forward and bootstrapped uncertainty; control false discoveries from 3 tested strategies.
- OOS expectancy, net profit factor and drawdown thresholds defined and measured on sufficiently large samples; stability with live-forward *observations* on the Gate testnet before requesting any write access.
- Integration with approved Stage 1 multi-timeframe context and centralized risk *as separate gated work*; do not wire research plans into OMS or the existing 300-agent roster yet.

**Stage 2A is an isolated deterministic validation foundation, not the final Stage 2, and it is not a profitability claim.**
