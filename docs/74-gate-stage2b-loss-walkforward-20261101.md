# Capital Cipher — Stage 2B Loss attribution and expanding walk-forward

Status: **read-only research branch; no Railway deploy, OMS action, LIVE, TESTNET order, or agent activation.**

Source: versioned S2 strategies and replay from PR #73. Production Gate remains on Stage 1 shadow commit `2eb009b`.

## New diagnostics

- Each completed simulated trade reconciles raw price-motion PnL, adverse slippage, fees, modeled funding, and net PnL. Reject discrepancies and nonfinite values.
- Identifies distribution of stop exits (including stop gaps/dual barrier), objectives and speculative end-window estimates. In-sample costs describe the observed candidate failures; never serve as independent OOS proof.
- Uses three expanding chronological folds for each public 800-bar series: TRAIN [0,400) → OOS [400,520); TRAIN [0,520) → OOS [520,640); TRAIN [0,640) → OOS [640,760). TRAIN selection considers trades from index 80 forward; latest OOS is never used to choose that fold's winner. Past OOS may be incorporated into later fold training under standard sequential walk-forward methodology.
- All strategy thresholds frozen, including minimum 5 in-sample completed trades, positive net return and PF>1, minimum 30 completed OOS trades for preliminary adequacy. If no qualifying strategy exists, abstain.
- Hypothetical fee assumption 6 bps/side + slippage 6 bps/side + funding 1bp/8h; note fees and execution differ across venue/tier, spreads and market regime.
- Research equity is independently reset to US$1000 per fold, so any aggregated OOS PnL is the **sum of separate account experiments** and never a continuous portfolio return.
- Public history is at most a few recent months depending on frame. Retrospective data receipt timestamps are not known. All claims about future alpha/profit are prohibited.

## Criteria before moving from S2B to strategy improvements

1. CI test regression and one-shot real Gate GET-only diagnostic pass.
2. Compare cost decomposition: losses before costs vs costs, stop and gap frequency, strategy differences.
3. At least one candidate must show credible net OOS evidence on sufficiently long non-overlapping prospective or historical periods; 800 bars is **not** enough for release.
4. Extend across volatility, trend, and sideways periods with time-locked multi-fold parameter choices and false discovery controls. Reevaluate actual Gate fee/funding/fill assumptions before any order routing request.
5. Keep Stage 1 24-hour reliability validation independent. Do not touch Railway services, DB volumes or core risk and OMS.

Research scores may be all negative; a negative or abstention result is an acceptable, honest outcome.
