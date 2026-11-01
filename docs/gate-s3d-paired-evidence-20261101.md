# S3D — paired descriptive evidence from a verified prospective journal

Research-only code on PR #80. **No merge, deployment, Railway mutation,
service wiring, orders or strategy promotion.** S1 stays on the original
`2eb009b` deployment until its audit finishes and its results are reviewed.
Audit scheduled: **2026-11-02 04:20 America/Cuiaba (08:20 UTC)**.

## Why this step

The existing S2D evaluator compares retrospective batches under a frozen
policy. This reader instead compares the recorded S3 observation journal,
after checking its hash chain, legacy outcomes and heads through the S3C
read-only reader. Source timestamps are journal records, not independently
attested exchange delivery or actual fills. Nothing fetches data or starts
collecting automatically.

For each independent symbol and strategy, compare STANDALONE, S1_CONFLUENCE
and cash (0), with identical capital, candle cutoff and observation clock.
Show realized PnL, open-position exit estimates after costs, completed trades,
open exposure, candle-close drawdown and difference versus both benchmarks.
Do not sum overlapping accounts or pool markets to inflate sample sizes.

Sample and numerical outcome are separate fields. For example, baseline −5
and filtered −2 is `REDUCED_LOSS_STILL_BELOW_CASH`, while filtered cash 0 with
zero trades is `MATCHED_CASH` and insufficient trader evidence.
A positive result above both benchmarks remains descriptive.

The measurement protocol V1 requires at least **96 recorded 15m candles
strictly after the frozen cutoff** and at least **30 closed trades in each
arm** for a preliminary descriptive sample. A stricter journal trade threshold
is respected; a weaker threshold cannot lower the floor of 30. The existing
freeze, 2026-11-02T00:00:00Z (01/11 20:00 MT), is unchanged. Warm-up and the
boundary candle are excluded. S3C's additive window metadata is calculated in
the same read transaction as replay and valuation.

These floors do **not** establish statistical significance, independence,
enough market regimes, robustness or profitability. They never authorize risk
changes, automatic ranking, discard, promotion or TESTNET/LIVE orders.
No policy is tuned from this report. A new strategy needs a new version and
a new untouched evaluation window; reusing this window for tuning would
invalidate its interpretation as unseen validation.

## Run only on an explicitly supplied research journal

```bash
cd backend
python -m app.strategy.gate_s3_paired_evidence /explicit/research/s3-forward.sqlite3
```

Outputs JSON to stdout; no new journal, export file, secret, schedule or host
service is created. Missing, corrupt or incompatible input exits with code 2.
No eligible accounts returns that explicit status, never invented performance.
SQLite read-only/WAL behavior and source limitations are inherited from S3C.
Last-recorded candle marks can be stale and do not measure intrabar drawdown
or exchange liquidation/mark-price behavior. Results currently cover 15m
entries with 1h/4h context, not separate validated DAY/SWING entry systems.

## Checks and next operational dependency

Local suite: **108 passed, 1 skipped**, including 15 new paired-evidence cases.
Tests use fabricated observations in isolated temporary journals, not actual
future data or proof of profit. Cases include an open loss, reduced losses
still below cash, sparse positive results, 95/96-candle boundary, 30-trade
floor, inactive filtering, stronger/weaker journal thresholds, pre-freeze
records, no pooling markets, unchanged source bytes, corrupt input and CLI.
Dedicated CI and full monorepo CI must pass on the published head.

The durable S3 collector is still not wired to the running Railway service.
No forward S3 performance result is claimed. After S1 homologation, review
the prepared S2E passive release; durable S3 integration needs its own runtime
and storage validation before continuous prospective evidence can accumulate.
Then evaluate strategies over new data and regimes before the AI experiment
and professional-selection stages. Stability alone is not economic evidence.
