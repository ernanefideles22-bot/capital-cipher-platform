# Month 11 — prolonged shadow validation

Month 11 expands the governed runtime from 200 to exactly 300 analytical
PAPER agents and adds a deterministic campaign that validates the platform
over a replay timeline equivalent to at least seven days. It proves
reconciliation, risk invariants, latency, bounded failure rates and safe
degradation without submitting an order.

LIVE execution is still absent. The campaign service has no decision, risk
approval, order submission, cancellation or credential interface.

## Acceptance boundary

The month is complete only when all of these invariants pass:

- exactly 300 enabled PAPER agents: 3 PRIMARY and 297 SHADOW;
- 100 new deterministic, read-only OHLCV diagnostics with unique capabilities;
- replay covers at least 673 ordered 15-minute candles and exactly seven days;
- each healthy checkpoint executes the complete 300-agent registry;
- error rate is at most 1% and output p95 latency at most 2,000 ms;
- every checkpoint runs OMS-to-adapter reconciliation;
- critical reconciliation mismatch count remains zero;
- risk state and the risk-limit fingerprint remain unchanged;
- OMS order and PAPER trade counts remain unchanged;
- an optional broker outage suspends shadow work in `DEGRADED` mode;
- a critical database outage suspends all campaign work in `SAFE_HALT`;
- critical recovery requires three healthy confirmations;
- final and checkpoint evidence is append-only in the private schema;
- administrative HTTP APIs are read-only and require `X-API-Key`;
- the 52 JSON Schema v1 contracts and the monorepo quality gate pass.

## 300-agent cohort

The three existing primary agents retain their authority. The 297 shadow
agents remain evidence-only. Month 11 adds ten windows for each of ten
diagnostic families:

| Family | Count | Diagnostic |
|---|---:|---|
| Downside deviation | 10 | downside semideviation |
| Upside deviation | 10 | upside semideviation |
| Return skew | 10 | standardized third moment |
| Tail ratio | 10 | upper-to-lower return tail balance |
| Volume trend | 10 | recent/reference volume shift |
| Range expansion | 10 | recent/reference normalized range |
| Return acceleration | 10 | late-versus-early mean return |
| Drawdown depth | 10 | deepest peak-to-trough loss |
| Recovery strength | 10 | recovery from the local trough |
| Price-volume correlation | 10 | return/volume correlation |

The windows are `8, 13, 21, 34, 55, 72, 89, 120, 144, 180`. Evidence always
declares `read_only`, `decision_authority: false`, `risk_authority: false`
and `order_authority: false`. Registration rejects non-PAPER agents, and no
agent capability contains order or credential access.

## Campaign protocol

`ShadowCampaignDefinition` freezes the market identity, ordered replay
interval, candle count, checkpoint interval, dataset SHA-256, exact cohort and
acceptance thresholds. Its own definition hash detects later mutation.

The default acceptance replay contains 673 15-minute candles: the first and
last timestamps are exactly seven days apart. All candles enter the same
bounded `CandleStore`; every 96 candles, and once at the final candle, the
campaign executes a checkpoint through the production versioned
`AgentRuntime`. This yields eight checkpoints without claiming that every
candle executed all 300 agents.

Two isolated deterministic checkpoints validate degradation:

1. `BROKER` unavailable produces `DEGRADED` and zero agent executions.
2. `DATABASE` unavailable produces `SAFE_HALT` and zero agent executions.

The recovery coordinator then requires the declared confirmations before the
next healthy checkpoint. Faults are injected only into the campaign-local
coordinator. There is no fault-injection HTTP route and no mutation of the
application's operational coordinator.

## Risk and reconciliation

Before the campaign, a SHA-256 snapshot captures central risk state, durable
control state, open position exposures and the complete risk-limit contract.
Every checkpoint reconciles the OMS with its active PAPER adapter before any
agent execution. A critical mismatch fails the checkpoint and the existing
reconciliation service independently activates the kill switch.

Final acceptance requires identical initial and final risk-state hashes,
identical risk-limit hashes, and unchanged OMS order and PAPER trade counts.
The campaign never calls `RiskManager.check`, the decision engine, the PAPER
entry method or an exchange adapter submission method.

## Persistence and Supabase security

The local versioned migration is:

```text
supabase/migrations/20260720210756_create_shadow_validation_campaigns.sql
```

It creates `shadow_campaign_checkpoints` and `shadow_validation_reports` in
the private `capital_cipher` schema. Both tables are append-only through
`SECURITY INVOKER` mutation triggers. RLS is enabled. All table, sequence and
function privileges are revoked from `public` and, when present, `anon` and
`authenticated`. No grants are added. This is a local migration only until a
separate, explicit deployment approval.

## Read-only administrative API

```text
GET /api/v1/operations/shadow-validation/reports
GET /api/v1/operations/shadow-validation/checkpoints
```

Both routes require the administrator API key. There is deliberately no
campaign-start, mutation, degradation-injection or LIVE execution endpoint.

## Local acceptance run

From `backend/`:

```bash
python scripts/run_month11_shadow_validation.py
```

The script uses a fresh in-memory PAPER context, deterministic public-market
fixtures and no credentials. A passing report records eight reconciliation
runs, six full-cohort checkpoints, two safe suspensions, 1,800 total agent
executions, zero agent failures, zero critical drift and zero orders.

## Safe operating runbook

If any report is `FAILED`:

1. keep PAPER/TESTNET gates unchanged and do not enable new execution paths;
2. locate the first failed checkpoint by campaign ID and sequence;
3. inspect reconciliation critical mismatches before agent latency;
4. verify risk-state, risk-limit, order-count and trade-count hashes;
5. if degradation did not suspend execution, activate the existing kill switch;
6. correct the isolated cause and create a new immutable campaign identity;
7. never edit or delete prior evidence.

Month 12 may consume only a passed, immutable Month 11 report. Passing this
campaign is necessary but is not authorization for LIVE capital.
