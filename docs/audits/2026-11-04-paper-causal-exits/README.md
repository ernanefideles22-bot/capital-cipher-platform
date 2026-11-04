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

Positions now inherit `market_candle.exchange` at creation. Calls without a
source candle retain the existing Binance default. Without this companion fix,
the venue admission check would skip Bybit monitoring because older creation
hardcoded Binance. Historical stored records are not relabeled.

Source-venue regressions reproduce six failing Bybit scenarios before the fix.
They cover BUY/SELL stops and targets, audit provenance, foreign-venue rejection,
ongoing funding and the real BacktestingEngine replay path. Liquidation priority
and fees are verified for both Binance and Bybit. The selected source-venue,
causality, margin and backtest suites pass: 57 tests. Commit-specific full CI
with disposable PostgreSQL/Redis is tracked in PR #105; the earlier successful
CI run belongs to the preceding revision and does not approve this update.

This change does not deploy, change Gate/S3, rewrite stored trades, reset history,
change strategy/risk settings or authorize execution. A future measurement cohort
must identify its implementation version and retain earlier history separately.
Software correctness is not evidence of a financial edge.
