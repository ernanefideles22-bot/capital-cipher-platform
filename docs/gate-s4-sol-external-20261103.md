# S4: frozen SOL lead, external past-period challenge

Follow the post-hoc REGIME_LONG_CONTROL lead from #94 with one separately registered test. No rule changes, parameter grid, orders, runtime integration, S3 change, risk change, merge or deploy. Existing selected/reserved candles remain untouched financially.

## Source registration and preserved technical failure

Before any external capture/outcome, V1 freezes the exact rule, prior discovery journal/bytes, all nine selection-source hashes, period, windows, criteria, cost scenarios, benchmarks and statistics. Last allowed external close is one4h interval before the earliest first close in the selection archive. Attempt one requests6000 older candles (2021-05-13 through2024-02-07). Gate returns HTTP400: `Candlestick too long ago. Maximum10000 points recently are allowed`. It is SOURCE_UNAVAILABLE, not a financial rejection, and cannot be reset/retried. The raw error diagnostic and original journal remain preserved.

V2 is a distinct source-window protocol registered before seeing external PnL. It is permitted only for that preserved no-PnL retention failure, with a recorded HTTP400/10000-limit diagnostic and identical selection map/end date. It freezes3600 candles instead (2022-06-17 through2024-02-07), leaving a retention margin relative to the previously captured6000-candle selection block. Candidate rules remain identical. This is one lead and two source attempts, not an undisclosed profitable-period search. No automatic source substitution, synthetic fills or source retries after an available outcome are allowed.

Each capture is public GET-only via the existing whitelist, no credentials/private endpoints. Inputs and archived raw pages are hashed and independently mapped against prices, volume, close timestamps and receipt clocks. SOURCE_AVAILABLE must precede financial evaluation. A failed/missing source produces no fabricated zero-loss backtest.

This is an earlier external historical block, not forward validation. Nonoverlap is verified against the registered selection archive; globally unseen project history is not certified, prior searches are not fully corrected, and public history was already knowable. Current strategy selection happened after the period being tested. It can challenge historical robustness but cannot establish prospective profitability.

## Fixed rule and references

SOLUSDT4h, daily00:00UTC. Candidate buys when the last120-bar own log return is positive and120-return volatility is nondegenerate. No learned model. The matched-exit reference proposes LONG regardless of that regime. Both share rolling20-day sign invalidation,2ATR14 absolute stop, executed3R target,30-bar max hold, six-bar cooldown, three-bar expiry, drift<=0.5ATR and fixed full-hold planning-cost/target gate<=20%. Entry at i+2 OPEN; stress delay adds a bar. Stop wins ambiguous candles; gap brackets precede time/thesis exits. No same-bar reentry; only LONG is allowed. Source parity tests compare the candidate to the original control, including up/down trends and WAIT decisions.

Unchanged ResearchRisk:1000 initial capital per window/arm/scenario,0.25% planned stop risk,1x max notional,2% daily-loss and10% realized-DD entry gates. Gaps/costs can exceed planned risk. Nominal fees/slippage6/6bps per side and1 funding bp/8h; stress12/12 and2; third scenario adds entry delay. Funding is a fixed debit estimate, not historical settlements. Estimated boundary exits do not count as closed evidence.

References: cash; unfiltered entries with identical exits/risk; one1x perpetual buy-and-hold mark with modeled costs/funding. Buy-and-hold has unmatched exposure/risk, no simulated liquidation or risk-triggered exits, and is not spot PnL or proof of executable account returns. It is descriptive and is not silently used as a risk-matched alpha test.

V1 planned windows [253,3000),[3033,6000), but performed no financial evaluation. V2 uses [253,1800),[1833,3600), with33 bars between blocks. Budget per available source: six candidate replays, six matched-control replays and six buy-and-hold references. No portfolio summing across alternative scenarios.

## Financial and conditional statistical gates

Keep >=30 closed trades per block/scenario, positive closed/marked net, PF>=1.2, largest win share<=50%, candle-close DD<=5%, and marked net strictly above matched-exit control in both blocks/all three scenarios. No relaxation when a result is positive but insufficient.

Only after all financial gates pass: aggregate candle equity changes over initial equity by UTC close day (sum, not compounding); compare candidate with cash and matched-exit control in both folds. Six family slots (3 scenarios x2 benchmarks). Existing stationary bootstrap9999 replicates, mean blocks8/16/32 days, worst-block sensitivity, Holm and simultaneous one-sided Monte Carlo precision bounds. Assumptions of stationarity/mixing are not independently verified; no exact finite-sample or global-project search correction is claimed. If gates fail, tests remain NOT_EVALUATED with conservative p=1, not actual failed p-tests.

Even conditional diagnostic passage cannot authorize promotion, forward tracking or orders. Profitability_proven remains false. New forward evidence is required.

## Reproduction and tests

Use the exact commit. CLI V1: `scripts.gate_s4_sol_external`, register with --catalog and --discovery-root, then run. CLI V2: `scripts.gate_s4_sol_external_window_v2`, additionally register --failed-root pointing to the preserved V1 study. Keep the HTTP error diagnostic in that study's parent directory. No journal reset. `review` is read-only. A reproduction is not independent evidence; live capture can become unavailable as retention advances.

Tests cover frozen-lead parity, source/clock rejection, strict entry/risk and stop-first oracles, short refusal, independent buy-hold/funding arithmetic, daily aggregation, pre-capture registration, unavailable-source persistence, old/new nonoverlap boundaries, no retry, byte-preserved failure/end date, non-retention adaptation refusal, exact V2 windows and statistical passage that still cannot promote. Existing S2/S3/S4 regressions remain required.

## Recorded outcome

V1 source attempt: unavailable, no financial replays. V2:3600 candles/9 public responses verified; complete data from2022-06-17T16:00Z to2024-02-07T12:00Z, strictly before selection's2024-02-07T16:00Z first close. Two folds, six candidate and six matched-control replays, six holding references; no technical evaluation failures.

| Block | Scenario | Candidate closed trades | Candidate closed net USDT | Profit factor |
|---|---|---:|---:|---:|
| 1 | nominal | 28 | -19.376 | 0.463 |
| 1 | stress | 29 | -23.275 | 0.408 |
| 1 | stress+delay | 26 | -12.239 | 0.662 |
| 2 | nominal | 37 | 4.997 | 1.104 |
| 2 | stress | 38 | 1.249 | 1.023 |
| 2 | stress+delay | 36 | 14.386 | 1.336 |

Each simulation starts with1000 USDT independently. Boundary marks are separately retained; they do not change the failed gate. Block one is net negative in all scenarios and below30 trades; nominal candidate also trails matched-exit control. Block two is positive but nominal/stress PF remain below1.2. The full fixed financial screen fails. Conditional bootstrap tests are therefore not executed; six slots remain NOT_EVALUATED/p=1 conservatively. No approved/forward candidate or profitability claim. Close-mark DD is not intrabar maximum loss, and buy-and-hold reference has unmatched risk and large drawdowns.

Keep the post-hoc SOL lead rejected by this external challenge. Positive cells cannot replace the full predeclared criteria. Do not alter thresholds or open the old reserve to rescue it.
