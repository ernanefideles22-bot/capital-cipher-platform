# S4: one pooled regime forecast, frozen before purged temporal evaluation

Following #93, test one low-complexity learned entry filter with fixed exits and references. Research only: no legacy edits, runtime agent registration, S3 integration, risk change, OMS writes, merge or deploy. A fitted statistical predictor is not a validated autonomous trader.

## One registered configuration

Three production-public 6000-candle BTC/ETH/SOL 4h sources from the already researched archive. Register inputs, code hashes, six features, one penalty, fit/evaluation boundaries, all arms, costs and criteria before fitting or measuring PnL. One pooled model fit, zero hyperparameter grid/refits. All failures and skipped opportunities remain recorded in an append-only six-event journal (REGISTERED, STARTED, FIT/FIT_FAILED, three RESULT/FAILED cases).

Fit uses daily 00:00 UTC decisions in [220,1200), only when the full label is available before 1200. Label: raw five-day price change from OPEN i+2 to CLOSE i+31. Features: 1/5/20-day log returns divided by trailing 20-day return volatility and sqrt(horizon), log(1-day/20-day return volatility), 20-day path efficiency, and 1-day x20-day normalized-return interaction. Clip normalized returns/volatility log at +/-5. Learn each feature's mean/std from fitting rows only; clip standardized features at +/-5. Population std floor 1e-12. No asset identity or future regime labels.

Ridge minimizes mean squared error plus 0.1 times squared non-intercept coefficients. Intercept is unpenalized. Solve the seven-parameter linear system with pivoted elimination, standard library only. Require >=120 rows and >=40 per registered market. Coefficients, scaling, row digest and maximum matured label index are serialized and hashed once, then frozen.

Two fixed blocks [1233,2400), [2433,3600), with 33-bar gaps. Model fit, scaling and labels cannot inspect those blocks; no refit after block one. Forecast labels reported within each block require a complete 30-bar outcome in that block. These dates were already inspected by project research: current model temporal separation is not globally untouched OOS. Labels overlap, assets correlate, and fitting row count is not effective independent sample size.

## Four arms and unchanged execution

- REGIME_RIDGE_4H_V1: propose only when absolute forecast >=0.0078 (the fixed stress five-day planning-cost proxy) and its sign matches the current 20-day return regime.
- MEAN_FORECAST_CONTROL: replace fitted prediction with the constant fitting-label mean, same threshold/regime.
- MOMENTUM20_CONTROL: original frozen daily 20-day normalized momentum rule.
- REGIME_LONG_CONTROL: always propose LONG when 20-day own return is positive, with the same daily clock and price/cost geometry; it is not buy-and-hold.

All use the same new isolated execution engine preserving the prior rules: 2ATR stop, 3R executed target, max30-bar hold, six-bar cooldown, three-bar setup expiry, rolling 20-day sign invalidation. All use strictly later entry/invalidation opens, stop-first ambiguity, gap-bracket priority, no same-bar reentry, drift<=0.5ATR and the unchanged fixed cost-affordability gate at proposal/fill. One intervening full candle can invalidate entry. END_WINDOW_ESTIMATE is excluded from completed-trade evidence.

Existing ResearchRisk remains 1000 starting equity per arm/block/scenario, 0.25% planned stop risk, <=1x notional, 2% daily-loss/10% realized drawdown entry gates. Gaps/costs can exceed planned stop risk. Nominal:6/6 bps fee/slippage per side plus1 bp/8h funding. Stress:12/12 plus2; third adds an entry bar. Funding and costs are assumptions, not historical brokerage fills. The 78bps planning proxy includes15 funding intervals for120h max hold; it is not a bound on realized costs or a forecast of PnL.

Budget:3 markets x2 blocks x4 arms x3 scenarios=72 replays (18 candidate,54 control). Candidate screen retains previous >=30 closed trades per block/scenario, positive closed and marked net, PF>=1.2, largest win<=50%, close-mark DD<=5%. Require candidate marked net strictly above all three controls in each block/scenario, as well as cash positivity. Comparisons measure whole policies with differing exposure/turnover; they do not isolate a causal coefficient effect. Screen success cannot authorize S5/forward tracking/orders. No significance test or correction for all past project searches is claimed.

## Observed result

One fit on474 daily labels,158 per market; latest label1197, before1200. All72 replays completed, no fit/evaluation technical failures. Zero market cases passed. Candidate had only0–4 completed trades per block/scenario. Predictive MSE skill vs constant was negative in all six market/block cells (about -3.4% to -30.0%): adding fitted features did not improve this historical squared-error metric.

The constant fitting forecast was about -0.2836%, below the fixed0.78% threshold, so its financial control abstained; zero trades are not a missing-data failure. The candidate's threshold/regime conjunction sharply reduced opportunities. This version is rejected rather than retuned on these outcomes.

REGIME_LONG_CONTROL on SOL had positive marked/closed net in both blocks under all three scenarios, but only19–25 completed trades per scenario. This is an exposed, post-hoc research lead, not an approved candidate or independent validation. No control is silently promoted. Its performance depends on market exposure/regime and requires a separately registered independent test, adequate sample and benchmarks. No unused historical reserve was opened to confirm it.

## Evidence boundaries and reproduction

All outcomes stay below3600. The later40%, including [4800,6000), is not replayed; original globally reserved [5400,6000) is preserved by this runner. Full-file source hashes are integrity checks, not financial evaluation. Previous studies/journals remain valid and unchanged.

From backend, exact version:
```
python -m scripts.gate_s4_regime_signal register --root NEW_ROOT --catalog ARCHIVE/history/catalog.json --output manifest.json
python -m scripts.gate_s4_regime_signal run --root NEW_ROOT --source-root ARCHIVE/history --output review.json
```
Do not reset an existing journal. `review` is read-only. Reproduction is not independent evidence.

Tests include an independent closed-form ridge solution with unpenalized intercept, immature/wrong-horizon labels, fit-only means/scales and future-tail independence, model availability, regime/control distinctions, mirrored geometry, common risk and stop-first execution for all four arms, one fit/72 fixed comparison slots, preserved failed fitting, phase order, no retry and inability to promote even when toy screens pass. Existing S2/S3/S4 regressions remain required.

Method references (not validation of this exact crypto implementation):
- https://scikit-learn.org/stable/common_pitfalls.html
- https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html
- https://www.nber.org/papers/w31502

No scikit-learn dependency or third-party model service was added.
