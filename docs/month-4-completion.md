# Month 4 — realistic research backtester complete

Month 4 is complete for the technical scope defined by the roadmap:
historical market replay, explicit costs, observed spread/funding data,
slippage and volume impact, isolated margin and liquidation, leakage-resistant
walk-forward evaluation, durable artifacts, and pre-registered out-of-sample
acceptance gates.

All outcomes remain `RESEARCH_ONLY`. A `PASS` means only that the candidate
passed the declared research gate and may proceed to later PAPER/shadow
evaluation. It cannot enable a strategy, change risk limits, switch system
mode, or authorize live execution.

## Completion evidence

| Requirement | Implementation |
| --- | --- |
| Historical candles | Content-addressed candle datasets and deterministic replay |
| Fees and slippage | `realistic-v1` adverse fills and explicit cost ledger |
| Historical spread/funding | `historical-execution-v1` as-of observations with provenance, checksum, staleness gate, and no future lookup |
| Margin and liquidation | `isolated-margin-v1`, leverage ceiling, maintenance margin, conservative intrabar liquidation, and liquidation fees |
| Walk-forward | Ordered train/validation/test folds, embargo, non-overlapping test windows, and isolated pipelines |
| Train-only fitting | Versioned fitter boundary receiving only the train slice; the current strategy is intentionally frozen |
| Statistical gates | Pre-registered minimum samples, performance/risk thresholds, exact sign test, candidate budget, and Bonferroni correction |
| Durable evidence | Append-only PostgreSQL/SQLite artifacts with content checksum |
| Schema lifecycle | Supabase CLI pinned and a reviewed, versioned PostgreSQL migration exercised in CI |

## Historical execution data

`BacktestRequest.historical_execution` accepts an ordered observed dataset:

```json
{
  "dataset_version": "historical-execution-v1",
  "source": "binance.archive",
  "exchange": "BINANCE",
  "symbol": "BTCUSDT",
  "max_age_seconds": 28800,
  "observations": [
    {
      "observed_at": "2026-07-01T00:00:00Z",
      "half_spread_bps": 1.7,
      "funding_rate_bps_per_8h": 0.8,
      "source_record_id": "binance-btc-20260701-0000"
    }
  ]
}
```

Observations must be strictly ordered and have unique timestamps and source
record identities. The resolver uses only the newest observation at or before
simulation time. Missing or stale coverage fails the run instead of silently
falling back to a constant. Funding is integrated piecewise when rates change
during an open position.

The full dataset receives a canonical SHA-256 identity. Reports and
walk-forward experiment identities include its manifest, so changing any
spread, funding, timestamp, source identity, or staleness rule creates a
different experiment.

When no historical execution dataset is supplied, the existing explicit
`realistic-v1` assumptions remain the documented deterministic fallback.

## Isolated margin and liquidation

Backtests record `isolated-margin-v1`:

```json
{
  "model_version": "isolated-margin-v1",
  "leverage": 2,
  "maintenance_margin_ratio": 0.005,
  "liquidation_fee_bps": 10
}
```

The requested leverage cannot exceed the central simulated risk limit. The
position notional still comes from stop-based risk sizing; leverage reduces
required isolated margin and does not multiply the approved risk amount.

The deterministic approximation is:

```text
long liquidation  = entry × (1 - 1/leverage + maintenance ratio)
short liquidation = entry × (1 + 1/leverage - maintenance ratio)
initial margin     = position notional / leverage
```

If one OHLC candle touches both stop and liquidation, liquidation wins. If the
candle opens beyond the liquidation threshold, the worse opening price is
used. Exit spread, slippage, fees, funding, and the separate liquidation fee
remain in the cost ledger and report.

## Train-only fitting

Every fold calls the versioned `frozen-strategy-fitter-v1` interface with only:

- the train segment metadata;
- the train candle slice;
- the pre-registered strategy parameters.

Its immutable artifact records the train dataset hash, row count, parameter
hash, and train-only diagnostics. Validation and test candles are never passed
to the fitter.

The current rule-based strategy has no trainable parameters, so the built-in
fitter deliberately preserves them. This closes the fitting boundary without
pretending that a model was trained. A future trainable candidate must
implement the same interface and produce a new versioned fitter identity.

## Pre-registered acceptance

The request fixes a research plan and acceptance criteria before any fold is
replayed. Defaults require:

- at least 3 folds and 30 trades per phase;
- at least 60% profitable folds;
- non-negative median PnL percentage and mean expectancy;
- worst drawdown no greater than 10%;
- zero liquidations;
- an exact one-sided profitable-fold sign test;
- family-wise alpha of 5%;
- Bonferroni adjustment by the declared candidate budget.

Validation and test are evaluated separately. The report is `PASS` only when
both pass. Every failed condition is returned as an explicit reason.

`candidate_index` cannot exceed `candidate_budget`, and the full plan and gate
criteria are part of the v2 experiment identity. Changing a threshold after
seeing results necessarily creates a new experiment.

## Versioned Supabase migration

The repository pins Supabase CLI `2.109.1` and tracks:

```text
supabase/config.toml
supabase/migrations/20260720065032_create_walk_forward_experiments.sql
```

The migration creates the private `capital_cipher` schema, append-only
artifact table, structured indexes, checks, RLS defense in depth, least
privilege revocations, and a `SECURITY INVOKER` mutation-rejection trigger.

CI applies the SQL to a disposable PostgreSQL 16 service before the application
bootstrap and verifies persistence, RLS, trigger behavior, and function
security. This work does not link to or run `db push` against a hosted
Supabase project.

## Safety boundary

Month 4 completion is evidence that the research engine implements its stated
mechanics and failure modes. It is not evidence that any strategy is
profitable, robust, or ready for real capital. PAPER remains the only
operational mode, and subsequent roadmap months must keep risk, execution, and
promotion authorities separate.
