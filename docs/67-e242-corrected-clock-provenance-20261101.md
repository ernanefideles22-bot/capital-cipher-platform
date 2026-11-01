# E2.4.2 — Correct the clock semantics of the E2.4 provenance alert

## Corrected interpretation of the 90/90 E2.4.1 findings

At commit `c78ce37`, Railway's read-only E2.4.1 audit returned:

- 90/90 sampled reference prices matched persisted candle closes.
- 90/90 target/outcome prices matched persisted candle closes.
- 90/90 candle `received_at` values preceded the corresponding forecast
  or outcome `created_at` values.
- 90/90 candle `recorded_at` values appeared later than the forecast or
  outcome `created_at` values.

**The last observation was misinterpreted as a causality failure.**
The repository's `AgentEvaluation.observe()` deliberately assigns
`AgentForecast.created_at = candle.received_at`; `settle()` likewise sets
`AgentForecastOutcome.created_at = candle.received_at`. These fields
record the candle arrival timestamp, NOT actual forecast/outcome database
insertion time. Therefore `candle.recorded_at > forecast.created_at`
does NOT mean a forecast was persisted or produced before the candle.

The operational `OrchestratorService.on_candle_closed()` source calls
`await repository.save_candle(candle, quality)` BEFORE invoking
`AgentRuntime.execute_many()` and then `AgentEvaluation.observe()`.
The runtime constructs `AgentExecutionJob.created_at=utcnow()` at
submission. Unlike the forecast's historical `created_at`, this clock
is a valid downstream timestamp for a bounded read-only consistency
crosscheck, though system clocks and transactions must still be trusted.

## E2.4.2 additional evidence

A bounded recent-event SQL query gathers earliest persisted
`agent_execution_jobs.created_at` by unambiguous `correlation_id`.
Compare the reference market observation's `recorded_at` and
`received_at` with the earliest runtime job submission time.

- `reference_persisted_before_first_agent_job` supports the expected
  operational ordering in the sampled cycles.
- `reference_not_proven_before_job` is an exception requiring deeper
  examination, not proof of fabricated market data.
- `no_matching_runtime_job` and ambiguous correlations remain unknown.
- The target/outcome candle remains OUT OF SCOPE for this crosscheck,
  since it belongs to the NEXT candle cycle with its own correlation ID.
- This does not authenticate the external exchange, ensure the agent
  received fresh book quotes, demonstrate statistical skill, prove
  profit, or authorize a strategy promotion.

The earlier E2.4 time comparisons remain available for forensic context
but the report explicitly labels them `legacy_recorded_at_vs_forecast_created_at_is_causal_proof=false`.
No data was rewritten. No LIVE/TESTNET/OMS/PAPER backend or agent modification.
