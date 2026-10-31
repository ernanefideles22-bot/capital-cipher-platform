# Capital Cipher — Phase D2: isolated agent-owned candidate replay

**Status:** research prototype, stacked on D1 PR #61 -> D0 PR #60.
This PR does **not** operate, promote, train or schedule the 300 agents.

## D2 implementation

- `candidate_replay.py`: executes only the agent hypothesis's own
  **approved deterministic predicates** against historical closed/received
  candles. It rejects unimplemented features (notably H4 alignment),
  invalid datasets, gaps, negative/NaN values and mismatched symbols.
- At a bar close, a signal becomes eligible at its `received_at`. Its
  simulated entry occurs at the first **later bar open whose timestamp is not
  before that receipt**. With realistic post-close receipt latency, this
  can be two bars after the original signal. No same-candle signal-and-entry.
- Brackets are set from ATR and entry price. If stop and target both touch
  within one candle, assume the adverse stop was first; adverse gaps through
  a stop execute at the open. Favorable target gaps receive no extra
  improvement. Exit-invalidation only triggers a later eligible open.
- Cost model: >=12 basis points **per side** all-in execution penalty,
  no separate double deduction. 1x capped notional, <=1% account equity
  initial risk per trade, conservative fixed position ownership. End-of-run
  open exposure is closed and marked `REPLAY_END`.
- `OneWayExposureGuard`: fail-closed **single owner per symbol**; no
  opposite or duplicate independent books. This is a deliberately stricter
  prototype than Gate one-way netting, which can aggregate/reduce positions.
  The simulator does not yet net real multi-strategy portfolios.
- `candidate_walk_forward.py`: uses the existing non-overlapping,
  chronological train/validation/test windows with embargo, tests the
  same immutable candidate's **own entry predicates** in separate windows
  with past-only lookback. Counts trades, P&L, drawdown and applies an
  exploratory Bonferroni-adjusted sign test; records result with D1 memory.
  No fitting/tuning on test windows. Result is `RESEARCH_ONLY`, always
  `execution_authorized=False`.
- Tests include lookahead guards, missing/duplicate bar rejection,
  execution delay, stop-first behavior, costs, one-way conflicts,
  idempotence and walk-forward segments.

## Honest limitations — do not mistake research for deployable strategies

1. OHLCV bar execution is an **approximation**; spread, exchange fees,
   slippage, partial fills, depth, market impact, funding and tick-order
   uncertainty need historical venue-aware calibration. The 12 bps/side
   assumption is a stress floor, **not verified Gate pricing**.
2. Exit risk/target logic is deterministic, not equivalent to a confirmed
   stop order on an exchange. The Gate stop homologation remains independent.
3. A fixed one-symbol single-book test does not yet prove multi-asset or
   multi-timeframe portfolio netting correctness.
4. Existing D0 indicators are simplified descriptions, not validated
   evidence of alpha. H4 alignment and arbitrary new indicators are
   deliberately rejected until their as-of data dependencies are modeled.
5. There is **no sealed final holdout**, authenticated evaluator signature,
   external hash anchor or protected historical data catalog yet. A positive
   score, even with a small p-value, cannot authorize PAPER or LIVE.
6. Multiple folds with shared train/validation histories may have dependent
   outcomes. The naive binomial sign test is exploratory and should not be
   represented as a definitive independent significance estimate.
7. Current agent forecast scorecards and PAPER closed-order attribution
   require privileged, source-backed joins before affecting weights.
8. This is **not self-learning** yet: agents may generate bounded hypotheses,
   but automated tuning, candidate search budget enforcement and
   privilege-isolated evidence attestation remain to be implemented.

## Next critical improvements before running 300-agent campaigns

- D2.1: reconcile model execution with actual historical Gate bid/ask,
  fill rules, funding and one-way aggregated positions across timeframes.
- D2.2: preregister research hypotheses + candidate search-budget in a
  privileged backend store, prevent duplicate/holdout reuse and measure
  independent sealed holdout *once* after research selection.
- D2.3: signed evaluator evidence and durable PostgreSQL append-only journal;
  source snapshot hashes anchored outside mutable research infrastructure.
- D3: historical agent decisions linked to downstream PAPER risk checks,
  entry/exit fills and all transaction costs.
- D4: ONLY after statistical review, a separated human-approved SHADOW
  campaign; retain manual approval to grant PAPER/TESTNET/LIVE.
- Keep Gate writes disabled and central OMS/risk untouched.
