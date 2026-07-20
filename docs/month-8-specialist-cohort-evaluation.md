# Month 8 — specialist cohort and observational evaluation

## Outcome

The governed runtime contains exactly 100 PAPER agents:

| Cohort | Count | Authority |
|---|---:|---|
| Existing primary decision agents | 3 | candidate evidence only |
| Existing shadow specialists | 37 | no decision authority |
| New technical specialists | 20 | no decision authority |
| New derivatives specialists | 15 | no decision authority |
| New macro specialists | 10 | no decision authority |
| New on-chain specialists | 10 | no decision authority |
| New news specialists | 5 | no decision authority |
| **Total** | **100** | **orders still require central risk and OMS** |

All 60 Month 8 agents are narrow, deterministic or single-metric specialists.
They use the same versioned runtime, bounded retries, isolated execution
memory and PAPER-only contract introduced in Month 5.

Month 8 does not weight production decisions by historical performance. It
creates trustworthy measurements for that Month 9 work.

## Honest external evidence

Technical agents read only normalized OHLCV already accepted by the market
data quality pipeline. Derivatives, macro, on-chain and news agents do not
call vendors or exchanges directly. They consume `SpecialistEvidence`:

- immutable identity and source event identity;
- domain, metric and `GLOBAL` or symbol scope;
- finite numeric value and explicit unit;
- source, observation and receipt timestamps;
- quality score, payload SHA-256 and optional provenance URI.

The authenticated ingestion boundary is:

```text
POST /api/v1/agents/evidence
GET  /api/v1/agents/evidence
```

An adapter must normalize vendor-specific payloads before using this boundary.
If matching evidence is missing, stale, below the agent's quality threshold or
in the future relative to the evaluated candle, the agent returns `WAIT` with
zero confidence. No synthetic fallback, silent forward-fill or LLM guess is
allowed.

Month 8 metric adapters normalize values to the contracted `ratio` unit.
Evidence with a different unit returns `WAIT`/`UNIT_MISMATCH`; adapters must
convert percentages or vendor-specific scales before ingestion.

Macro evidence is globally scoped. Derivatives, on-chain and news evidence is
symbol scoped. Every external agent owns exactly one metric.

## Forecast evaluation

After all 100 agents complete a candle cycle, the observational evaluator:

1. settles forecasts whose one-timeframe horizon has matured;
2. records the realized price, return and direction;
3. records a new immutable forecast for each output;
4. exposes read-only forecasts and scorecards to administrators.

Signals map to probability of price increase as follows:

```text
BUY  = 0.5 + confidence / 200
SELL = 0.5 - confidence / 200
other signals = 0.5
```

The per-agent Brier loss is:

```text
(probability_up - realized_up)²
```

For a cohort forecast, marginal contribution is:

```text
leave_one_out_ensemble_loss - full_ensemble_loss
```

A positive value means including the agent improved the equally weighted
shadow ensemble for that realization; a negative value means it harmed it.
This is evaluation evidence, not causal proof or capital authorization.

Scorecards report directional accuracy, mean Brier loss and mean marginal
contribution. They remain `INSUFFICIENT_SAMPLE` until 30 settled observations.
The Month 8 APIs return `decision_authority: false`:

```text
GET /api/v1/agents/evaluation/forecasts
GET /api/v1/agents/evaluation/scorecards
```

## Persistence and security

Migration `20260720155902_create_specialist_evidence_evaluation.sql` creates
three private `capital_cipher` tables:

- `specialist_evidence`;
- `agent_forecasts`;
- `agent_forecast_outcomes`.

The tables are append-only, use immutable content identities, enable PostgreSQL
RLS and revoke `public`, `anon` and `authenticated`. The mutation function is
`SECURITY INVOKER` with an empty search path. SQLite development uses matching
update/delete rejection triggers.

Four language-neutral JSON Schemas extend the v1 manifest to 39 contracts:

- `specialist-evidence`;
- `agent-forecast`;
- `agent-forecast-outcome`;
- `agent-scorecard`.

The API has no endpoint to edit or delete evidence, forecasts or outcomes.
There is no exchange credential, order, risk-limit or execution capability in
the specialist or evaluator services.

## Failure semantics

- External evidence unavailable or invalid: only the affected agent returns
  `WAIT`; the cycle continues.
- Evaluation persistence unavailable: an `AGENT_EVALUATION_FAILED` event is
  logged, but observational evaluation cannot create, block, resize or route
  an order.
- Duplicate evidence or forecast identity: identical redelivery is
  idempotent; conflicting immutable content is rejected.
- Restart: persisted evidence, forecasts and outcomes are restored before
  market processing.

## Completion gates

Month 8 is complete only when all of these hold:

- registry validates exactly 100 PAPER agents, 3 primary and 97 shadow;
- category counts are exactly 20/15/10/10/5 for the new specialists;
- all 100 execute through the versioned runtime;
- technical outputs are deterministic and read-only;
- missing, stale, low-quality and unit-mismatched evidence fails closed;
- forecast accuracy and leave-one-out contribution are reproducible;
- scorecards cannot affect decisions or execution;
- SQLite and PostgreSQL enforce append-only evidence;
- private RLS/revocation and `SECURITY INVOKER` checks pass;
- all 39 contracts validate;
- complete monorepo and PostgreSQL/Redis CI gates pass.

No hosted Supabase project is mutated by this branch. Applying the migration
remains a separate reviewed deployment action.
