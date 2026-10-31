# Capital Cipher — D1: experiment memory and walk-forward baseline bridge

**Status:** research-only, stacked on PR #60; do not merge/deploy before review.
D1 does not enable PAPER, Gate, LIVE, trade automation or mutable agent weights.

## Implemented

### 1. Durable *offline* research memory
`app.strategy.research_memory.ResearchMemory` uses an append-only SQLite log,
stable event identity, hash chaining, event validation and triggers that reject
UPDATE/DELETE. Each strategy proposal is fingerprinted by its D0 immutable
parameters. A separate head hash should be retained in a trusted checkpoint
store; this local ledger is **tamper-evident, not tamper-proof**.

- Event kinds: HYPOTHESIS_REGISTERED, BASELINE_WALK_FORWARD_OBSERVED,
  PAPER_CLOSED_TRADE_OBSERVED and RESEARCH_NOTE.
- Identical event identity + identical payload is idempotent.
- Conflicting repeats, unknown event types or unregistered hypotheses fail.
- PAPER observations must identify the closed order/decision, report net P&L
  and exit reason. Attribution remains false until a separate privileged
  reconciler validates a signal -> decision -> risk -> order -> closure chain.
- No ORM migrations or new production writes. Local file is **not** a
  deployment persistence strategy (Railway container is ephemeral).

### 2. Baseline backtest connector
`BaselineResearchRunner` reuses the repository's existing
`WalkForwardEngine`, with a pre-registered baseline strategy and explicit
historical candles supplied by the caller. It requires:
- Same market/timeframe as the hypothesis
- The existing StrategyEngine's versioned candidate (no arbitrary strategy)
- A positive embargo, 1x leverage, >=12 bps/side of minimum assumed costs
- Consistent walk-forward report artifact integrity

Results are stored as `BASELINE_WALK_FORWARD_OBSERVED` with a full report
identity, dataset hash and gates. This is a **comparison baseline** only;
the novel hypothesis's predicates were **not executed** in that replay.

Both existing and new modules emit `execution_authorized=False`.

### 3. Unit tests
- Journal persistence, concurrent-safe append transactions, idempotence,
  hash integrity, tampering and external checkpoint checks.
- Self-claimed causal PAPER attribution rejected and unregistered events rejected.
- A mocked WalkForwardEngine ensures the bridge annotates baseline-only
  results; invalid costs or candidate identities are rejected.
- The monorepo CI runs the actual tests in `backend/app/tests`.

## Next: D2 must implement actual candidate replay

A candidate MUST NOT be promoted because a baseline passed. The next engine
must interpret a strategy's *own* whitelisted features on as-of candle data,
use next-bar executable fills and conservative within-bar stop/target ambiguity
resolution, and control overlapping same-symbol exposure according to venue
one-way netting or explicitly isolated books. Walk-forward selection may use
train/validation only; a sealed holdout is read once per finalized candidate.
Cost/volume/funding stress and multiple-testing correction are mandatory.

Then add authenticated, privilege-separated evidence attestations and a
PostgreSQL append-only store with externally anchored integrity checkpoints.
Research agents must not have write access to the trusted results store,
execution API or approval gate.

**Agent learning** in this phase is not automated training. D1 records history
for future controlled improvement; hypothesis generation, actual candidate
replay and automatic strategy optimization have NOT been enabled.
