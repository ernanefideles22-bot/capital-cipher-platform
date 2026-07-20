# Month 9 — portfolio construction, weighted consensus and drift

Month 9 expands the governed PAPER runtime from 100 to exactly 150 agents and
adds four controls between the static primary decision and central risk:

1. immutable, versioned consensus experiments;
2. out-of-sample performance eligibility and bounded agent weights;
3. rolling drift detection by agent version;
4. advisory portfolio targets that can only reduce central-risk sizing.

The central Risk Manager, single-use approval and OMS remain mandatory. None
of the new agents or services can submit, cancel or reconcile orders.

## 150-agent cohort

The 50 new deterministic shadow agents read only normalized OHLCV:

| Family | Count | Purpose |
|---|---:|---|
| Return horizons | 8 | independent momentum horizons |
| Volatility shifts | 8 | recent versus reference volatility |
| Drawdown windows | 6 | distance from rolling peak |
| Volume pressure | 6 | signed participation |
| Range efficiency | 6 | net move versus path length |
| Tail balance | 5 | upside versus downside squared returns |
| Return autocorrelation | 5 | lag-specific persistence |
| Liquidity shifts | 6 | dollar-volume/range capacity proxy |

All 50 are `PAPER`, `SHADOW`, deterministic and read-only. The runtime now
contains 3 primary and 147 shadow agents.

## Statistical eligibility

The default experiment is `SHADOW`. An agent version is eligible for weighted
consensus only when all configured gates pass:

- at least 100 settled out-of-sample forecasts;
- at least 50 directional observations;
- accuracy at or above the experiment threshold;
- Brier loss at or below the experiment threshold;
- strictly positive marginal contribution;
- no critical drift for that exact agent version.

Eligible scores are normalized with a per-agent concentration cap. The
resulting weights must sum to one. When fewer than the minimum eligible agents
remain, the artifact is `INSUFFICIENT_DATA` and the static Month 8 decision is
preserved.

## Conservative decision overlay

Only an explicitly activated `CONFIRMATION` experiment may affect a candidate:

- it cannot create a direction from `HOLD`, `WAIT` or `BLOCK`;
- it cannot reverse `BUY` to `SELL` or `SELL` to `BUY`;
- it cannot increase confidence;
- agreement preserves the primary candidate;
- disagreement or neutrality tightens a directional candidate to `WAIT`;
- any service or persistence failure falls back to the static primary engine.

Experiment definitions never mutate. `CREATED`, `ACTIVATED` and `RETIRED`
lifecycle records are independent append-only events.

## Drift

For each agent version, the monitor compares a non-overlapping reference
window with a recent window. It records changes in directional accuracy,
Brier loss and marginal contribution. Warning thresholds are half the
configured critical thresholds. A critical observation excludes only that
agent version from weighting; it does not grant the drift monitor order or
risk authority.

## Portfolio construction

For a directional candidate, the constructor derives a target weight from
primary confidence and applies the tightest of:

- configured maximum target weight;
- remaining gross and net exposure;
- remaining symbol and strategy exposure;
- maximum single-position exposure.

The resulting proposal has `decision_authority: false`. Its `max_notional` is
passed to central risk only as a tightening override. Central risk recomputes
all exposure, VaR, concentration, leverage, drawdown and approval rules. A
zero proposal reaches central risk as an explicit block; no order can bypass
the normal approval path.

## Storage and access

The private `capital_cipher` schema adds:

- `consensus_experiments`;
- `consensus_experiment_events`;
- `weighted_consensus_snapshots`;
- `drift_observations`;
- `portfolio_proposals`.

All five tables are append-only, RLS-enabled and revoked from `public`,
`anon` and `authenticated`. Foreign-key query columns and operational lookup
patterns are indexed. Five strict JSON Schemas extend the v1 manifest to 44
contracts.

Protected APIs:

```text
POST /api/v1/governance/experiments
POST /api/v1/governance/experiments/{experiment_id}/events
GET  /api/v1/governance/experiments
GET  /api/v1/governance/consensus
GET  /api/v1/governance/drift
GET  /api/v1/governance/portfolio-proposals
```

Every endpoint requires `X-API-Key`; an unset key locks the boundary.

## Completion gates

Month 9 is complete only when:

- the registry validates exactly 150 PAPER agents (3 primary, 147 shadow);
- all 50 new diagnostic agents are deterministic and authority-bounded;
- consensus requires at least 100 settled samples and positive marginal value;
- weight concentration is capped and weights sum to one;
- shadow experiments cannot change decisions;
- confirmation experiments can only preserve or tighten primary decisions;
- critical drift reproducibly excludes an affected agent version;
- portfolio construction can only reduce central-risk sizing;
- SQLite and PostgreSQL enforce append-only governance evidence;
- RLS, revocation, strict contracts and authenticated APIs pass;
- all 44 contracts and complete monorepo gates pass.

No hosted Supabase project is changed by this branch. Migration deployment is
a separate reviewed action.
