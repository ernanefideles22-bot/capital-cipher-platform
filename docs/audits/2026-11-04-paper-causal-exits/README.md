# PAPER exit interval admission

A monitoring candle must describe a complete interval after the position entry.
Without this boundary, a position opened on a smaller timeframe can close on
a larger timeframe using an open or wick from before entry.

The monitor now requires matching symbol and venue and computes a conservative
interval lower bound as `closed_at - TIMEFRAME_SECONDS[timeframe]`. It skips
bars whose lower bound precedes `opened_at`, before funding or exit handling.
Unknown durations and missing entry timestamps fail closed.

Whole post-entry candles retain the existing adverse gap reference, liquidation
handling and stop-first assumption when both stop and target appear in OHLC.
The duration subtraction supports inclusive exchange closes and synthetic exact
boundaries conservatively. An entry partway through a bar must wait for a whole
post-entry bar; finer data is required to reduce that blind interval. This does
not introduce ticks, intrabar chronology, new feeds or a global stream watermark.

Tests use synthetic prices, timestamps and positions. They cover pre-entry,
same-close and partly overlapping candles, full post-entry candles, adverse gaps
on BUY and SELL, stop-first ambiguity, venue isolation and invalid timing inputs.
Synthetic exit/recovery certification fixtures now advance a full interval after
entry; runtime clock/freshness and risk limits are unchanged.

Full local backend validation before synthetic-fixture redaction: 815 passed,
34 skipped (174.99 seconds). Revalidate the synthetic-monitor suite and use CI
with disposable PostgreSQL/Redis for skipped integrations.

This change does not deploy, change Gate/S3, rewrite stored trades, reset history,
change strategy/risk settings or authorize execution. A future measurement cohort
must identify its implementation version and retain earlier history separately.
Software correctness is not evidence of a financial edge.
