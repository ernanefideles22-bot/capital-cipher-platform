# E2.2 — Per-agent skill baseline and prospectivity prerequisite

## Scope

This research-only component reads versioned forecast/outcome pairs and execution
jobs from the existing PAPER staging database, in the same bounded READ ONLY /
REPEATABLE READ audit transaction. It does **not** execute agents, schedule a
collector, change weights or modify the operational OMS/PAPER/risk backend.

Input sources:
- `capital_cipher.agent_forecasts`: immutable agent/version/role/symbol/timeframe
  prediction, probability_up, forecast_at, target_at, created_at
- `capital_cipher.agent_forecast_outcomes`: settled realized_up/realized_at
- `capital_cipher.agent_execution_jobs`: agent/version, PRIMARY/SHADOW,
  execution status; this is a bounded job sample, not the full roster

Historical sample limits: most recent **3,000** forecast rows and **5,000**
runtime jobs created within the audit's 30-day lookback. Truncation is explicit.
Missing tables are labeled NOT_VISIBLE, **never zero**. Table access through
RLS may hide rows and must not be inferred as an empty registry.

An outcome counts only when the agent's forecast and processing creation
precede the target candle and its realized timestamp matches target_at.
Per-agent/version/universe/role metrics: count, directional accuracy, mean
Brier loss and improvement versus the constant-p=0.5 benchmark.
30 settled forecasts permit **descriptive** reporting, not independent
statistical power, causal profit attribution, strategy promotion or
risk-weight updates. Scorecards are ordered by data coverage, never cherry-picked
by winning score. One-bar predictive forecasts are highly correlated by candle;
deduplication and counterfactual opportunity/fill costs remain unresolved.

## Why this matters

E2.0 chart rules used real Binance SPOT candles but produced 3–5 trades and
mostly losses. E2.1 compared the actual three-core-agent BacktestingEngine
signal path (MarketData, Quant, Trend) against frozen chart rules, but **not**
all 300 runtime agents. This E2.2 diagnostic identifies whether individually
versioned runtime forecasts and verified outcomes exist to measure predictive
skill and whether the 300-agent registry is actually represented in evidence.

Do not extrapolate the 3-core-agent engine benchmark to the 300-agent system.

## Prospective release path

1. Review audit visibility, settled counts, role counts and timestamp integrity.
2. If records are absent, separately design and authorize a write-only,
   append-only **PAPER/SHADOW** capture mechanism with immutable candle input,
   candidate ID, version hash, timestamp and target, without enabling LIVE.
3. Pre-register hypotheses and obtain NEW as-of market data, withheld from
   strategy fitting. Validate first on historical unseen holdout then in
   a true forward PAPER shadow campaign. Preserve complete rejected candidates.
4. Analyze predictive skill **and** independent, cost-adjusted execution
   outcomes under a fixed, venue-realistic simulator. Use effective sample
   size, time-series dependence and multiple-testing correction across agents.
5. Only after proof and explicit user authorization may a separate operational
   PR be considered. Current rules and risk limits remain unchanged.

No prospective collector is running and this E2.2 read-only audit alone
does not establish profit or improvement. No Railway changes are applied
without review and 2FA.
