# Month 6 — central portfolio risk engine complete

Month 6 is complete for the roadmap scope: the platform now has one
portfolio-aware risk authority, immutable idempotent evaluations, expiring
single-use order approvals, durable kill-switch control, and exactly 40
governed PAPER agents.

This milestone controls the existing simulator only. It adds no testnet or
live execution, private exchange credentials, real order adapter, or LIVE
state.

## Completion evidence

| Requirement | Delivered behavior |
| --- | --- |
| Exposure | Gross, directional net, symbol, strategy, position and count limits |
| Concentration | Resulting symbol concentration is measured before approval |
| VaR | Historical portfolio VaR/expected shortfall when history is sufficient; conservative proxy and explicit warning otherwise |
| Strategy limits | Strategy profiles may tighten exposure and VaR; global limits always cap them |
| Idempotency | Same key and same immutable request return the same evaluation; conflicting reuse fails |
| Approval | APPROVED/REDUCED evaluations mint a content-bound, expiring, single-use capability |
| Order boundary | A reconstructed or modified `RiskCheck` cannot create an order |
| Atomicity | Durable approval consumption and PAPER order insertion share one short transaction |
| Kill switch | Serialized durable state, append-only events, active-approval revocation, startup restore and MAINTENANCE-only reset |
| Persistence | Four private `capital_cipher` tables, RLS, revoked public/API roles, checks, indexes and `SECURITY INVOKER` triggers |
| Scale milestone | Exactly 40 PAPER agents: 3 PRIMARY and 37 SHADOW |
| Compatibility | Month 4 backtesting and Month 5 runtime invariants remain regression-tested |

## Risk decision flow

```text
Decision + market/data state
  -> canonical request fingerprint
  -> idempotency conflict check
  -> operational/drawdown/loss/leverage gates
  -> proposed risk-based notional
  -> gross/net/symbol/strategy/position capacity
  -> concentration gate
  -> historical portfolio VaR
       -> conservative proxy when observations are insufficient
  -> APPROVED | REDUCED | BLOCKED | KILL_SWITCH
  -> audit + immutable risk evaluation
  -> expiring OrderApproval for APPROVED/REDUCED only
  -> exact payload validation
  -> atomic approval consumption + PAPER order insert
```

No order can advance when audit or risk-evidence persistence fails. A blocked
evaluation never carries an approval ID.

## Exposure and VaR

Every open PAPER order is represented by symbol, timeframe, strategy,
direction, notional and leverage. The proposed order is evaluated against the
resulting portfolio, not in isolation.

Historical VaR uses aligned simple returns from the normalized `CandleStore`:

```text
portfolio_pnl[t] = sum(signed_notional[i] * return[i,t])
loss[t] = max(0, -portfolio_pnl[t])
VaR = empirical confidence quantile(loss)
ES  = mean(loss >= VaR)
```

When any required series has fewer than `VAR_MIN_OBSERVATIONS`, the engine
applies a conservative volatility proxy to gross exposure and records
`VAR_PROXY_USED` plus `INSUFFICIENT_RETURN_HISTORY`. If lowering the proposed
size can meet the cap, the result is `REDUCED`; otherwise it is `BLOCKED`.

Defaults are bounded and configurable:

```text
MAX_GROSS_EXPOSURE_PERCENT=200
MAX_NET_EXPOSURE_PERCENT=150
MAX_SYMBOL_EXPOSURE_PERCENT=100
MAX_STRATEGY_EXPOSURE_PERCENT=100
MAX_SINGLE_POSITION_PERCENT=100
MAX_SYMBOL_CONCENTRATION_PERCENT=90
MAX_PORTFOLIO_VAR_PERCENT=5
VAR_CONFIDENCE=0.99
VAR_LOOKBACK=100
VAR_MIN_OBSERVATIONS=30
FALLBACK_VOLATILITY_PERCENT=1
RISK_APPROVAL_TTL_SECONDS=60
MAX_ENTRY_DEVIATION_BPS=100
```

Configuration validation rejects inconsistent leverage or VaR observation
bounds at startup.

## Idempotency and execution approval

The risk request includes the decision, price, ATR, data state, balance,
leverage, effective limits and current position identities. Its canonical
SHA-256 fingerprint plus the caller's idempotency key forms the evaluation
identity.

An `OrderApprovalV1` binds the evaluation, risk check, decision, correlation,
request fingerprint, symbol, timeframe, strategy, side, maximum notional,
leverage, reference price, entry deviation and expiry. The PAPER engine
accepts only the exact immutable check retained by the central engine.

PostgreSQL locks the singleton risk-control row and approval in a fixed order,
verifies that both still permit execution, marks the approval `CONSUMED`, and
inserts the order in the same transaction. An approval can transition only
from `ACTIVE` to `CONSUMED`, `REVOKED`, or `EXPIRED`.

## Durable kill switch

Triggering the kill switch serializes the singleton state, increments its
revision, writes an append-only event, revokes active approvals, forces an
operational state into `ERROR`, and records an audit event. Local state still
enters fail-safe mode if durable persistence raises.

On restart, risk control and open exposure are restored before PAPER is
allowed. An active durable switch boots into `ERROR`. Reset requires an
authenticated request, non-empty reason and explicit `MAINTENANCE` state:

```text
POST /api/v1/risk/kill-switch
POST /api/v1/risk/kill-switch/reset
```

There is no direct ERROR-to-PAPER recovery path.

## Forty-agent cohort

The original three decision agents remain the only PRIMARY agents. The 12
Month 5 specialists and 25 Month 6 specialists are SHADOW. New capabilities
cover return distribution, downside/upside behavior, drawdown, trend
efficiency, autocorrelation, skew/tails, gaps, range state, volume state,
price-volume relationships, candle structure, persistence, entropy, shock
recovery, freshness and multi-window consensus.

All 25 new agents read only normalized OHLCV through `CandleStore`, are
deterministic and versioned, and receive no `RiskManager`,
`PaperTradingEngine`, repository, exchange adapter or credential. They cannot
change action, confidence, warnings, risk or orders.

## Database and Supabase boundary

Migration:

```text
supabase/migrations/20260720083050_create_central_risk_engine.sql
```

Private tables:

```text
capital_cipher.risk_evaluations
capital_cipher.order_approvals
capital_cipher.risk_control_state
capital_cipher.risk_control_events
```

The private schema is not exposed by `supabase/config.toml`. RLS is enabled as
defense in depth; privileges are revoked from `PUBLIC`, `anon` and
`authenticated`; evidence is append-only; approval and kill-switch transitions
are guarded in PostgreSQL itself. SQLAlchemy installs equivalent protections
for direct deployments and SQLite tests.

No hosted Supabase project was linked or modified.

## Exit gate

Month 6 is complete only when all of the following pass:

1. exposure, concentration, VaR, reduction and global-limit tests;
2. idempotency conflict, forged check, expiration and single-use tests;
3. durable kill-switch restore, revocation and reset-state tests;
4. atomic approval/order persistence and immutable-evidence tests;
5. exactly 40 PAPER agents with 37 non-interfering SHADOW roles;
6. 27 versioned contracts;
7. the complete monorepo quality gate;
8. real PostgreSQL 16 migration, RLS, trigger and transaction checks in CI.

This does not prove profitability or readiness for real capital. OMS,
exchange testnet adapters and continuous reconciliation remain Month 7 work.
