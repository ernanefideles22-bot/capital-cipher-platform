# Capital Cipher — D2.2: Auditable venue tape and shared candidate scheduler

**Status:** offline RESEARCH-ONLY. Stacked on PRs #60, #61, #62 and #63. No change to Railway, existing LIVE/TESTNET/PAPER execution, main OMS, risk controls or agents.

## Delivered

1. `venue_research_catalog.py` — content-addressed archive of historical Gate-like contract specifications, bid/ask quotes, top-of-book depth, taker fees and funding. The source identity, archive ID, observation and receipt timestamps, quote/funding counts, SHA-256 records digest and full dataset digest are recorded. Existing files are not overwritten; loading recomputes both checksums and rejects altered or future-captured observations.
2. `candidate_replay.CandidateTrade` — adds separately preserved `entry_signal_received_at` and `exit_signal_received_at`. A protective stop/target is treated as a resting instruction known from the entry; close timing is still OHLCV approximate. Candidate reports record evaluated interval boundaries.
3. `candidate_venue_scheduler.py` — converts individual D2 candidate backtest trades to a chronological set of strategy target changes. Strategy positions for the same contract are summed into ONE netted contract quantity. Exit is deterministically prioritized before entry at equal timestamps. All venue intents have a source-signal availability timestamp. Quantities round down to integer contracts.
4. The portfolio compiler refuses strategy overlap that would silently reverse the shared book or where closing a hedge increases net exposure, unless an explicit **research-only** exception is passed. Such exits are not incorrectly marked `reduce_only`. Every result stays `execution_authorized=False`.
5. `compare_candidate_to_baseline` runs baseline and proposed-strategy simulations on the SAME archived quote/funding tape, with the SAME starting equity and required common evaluation window. Missing source timestamps, unavailable quotes, missing OOS coverage or mixed periods fail closed. P&L is **net of embedded spread/slippage, fees and modeled funding**.

## Important limitations

- **No authentic Gate historical quotes/funding have been obtained or imported in this PR.** Only tests use artificial fixtures. The tool checks dataset *integrity*, not external *authenticity*; it cannot prove a record came from Gate without an independently verified source, recorded actual availability and provenance.
- An unchanged complete D2 strategy snapshot is required. This module does **not** run 300-agent campaigns or generate missing source data.
- D2 generated stops/targets are OHLCV assumptions, not verified exchange stop triggers. The shared scheduler uses the resulting exit timestamps to price fills in the archived quote tape; it does NOT independently replay protective stop execution under actual mark/index/last trigger conditions.
- Each baseline and candidate must be evaluated for the same explicit evaluation interval on the same venue tape. The comparison remains exploratory; the old D1 baseline engine does not automatically prove strategy candidate results, and an in-sample optimized candidate can overfit.
- No sealed holdout, signed evidence attestation, historical queue reconstruction, fill partiality or margin-liquidation parity is present. A SHA-256 file checksum is NOT a trusted digital signature.
- For production, persist archives and audit heads in a privileged durable storage system. Do not rely on Railway's ephemeral filesystem or editable agent containers for evidentiary authority.
- A `reduce_only` position closure that would cause the netted book to INCREASE must not be submitted as reduce-only; default policy blocks it and requires an explicitly documented portfolio policy. TRUE exchange compatibility requires integration tests on an isolated authorized Testnet campaign.

## Next work before promoting any strategy

1. Collect real, independently verifiable venue archives at point-in-time frequency, including quote depth, actual timestamps, contract multiplier, funding, fees and stop-trigger mode.
2. Set a pre-registered candidate selection policy on train/validation ONLY; freeze a final holdout and store its one-shot read receipt under a separate trusted evaluator.
3. Cross-check same-contract, multi-timeframe netting and actual Gate order constraints against order/fill evidence. Include a portfolio-wide bracket policy rather than independent stops for opposing strategy exposures.
4. Perform **out-of-sample** scenario comparisons over independent market regimes, statistical correction for all candidate searches and realistic execution stress; evaluate alpha and capacity, not signal-count or high win-rate alone.
5. Keep SHADOW until human review; separate approval for any later PAPER, Testnet or LIVE stage.

## Commands for project reviewers

Run after installing backend development dependencies:
`python -m pytest app/tests/test_venue_research_d22.py app/tests/test_venue_research_execution_d21.py app/tests/test_strategy_candidate_replay_d2.py -q`

Do not connect this research subsystem to an execution scheduler without a separate audited change.
