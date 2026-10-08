# Bybit Futures TESTNET safety gate

Status: implementation gate. This document does not authorize TESTNET and does not introduce LIVE execution.

Frozen baseline revision: `e967c234220c673084ad3a9e618603b4ad3d52af`.

## Release rule

The Bybit Futures TESTNET path remains blocked until every control below is implemented, persisted, tested, and included in a new release-readiness evidence bundle for the final source revision. The current Railway staging service must remain `PAPER` while this work is in progress.

## Mandatory controls

### 1. Durable protected entry

An approved TESTNET order must persist the central-risk protection parameters before the execution command can be claimed:

- stop-loss price;
- take-profit price;
- leverage;
- `reduce_only` intent;
- exact exchange/environment identity.

A restart between approval and dispatch must reconstruct the same values. Missing protection on a Bybit entry is fail-closed and must quarantine/reject the submission before any exchange write.

### 2. Bybit protected order payload

The Bybit linear TESTNET adapter must send entries with explicit protection:

- `category=linear`;
- exact testnet host only;
- `reduceOnly=false` for an entry;
- persisted `stopLoss` and `takeProfit` from the approved risk check;
- full-position TP/SL mode with market protective execution;
- deterministic `orderLinkId`.

No production Bybit host is permitted.

### 3. Explicit leverage configuration

Before the first entry write for a symbol, the adapter must configure both buy and sell leverage to the approved leverage using the Bybit TESTNET position endpoint. Any transport ambiguity or venue rejection must fail closed; the order submission must not proceed unless leverage configuration is acknowledged or an explicitly safe idempotent 'already configured' response is recognized.

The configured leverage may never exceed the central-risk approval.

### 4. Reduce-only exits

Any explicit close/exit command must be represented as an exit, persisted as such, and submitted with `reduceOnly=true`. A reduce-only command must never increase exposure. Entry and exit intent may not be inferred from side alone.

Until this flow exists and is covered by tests, attached exchange TP/SL is the only accepted automatic protection and discretionary TESTNET exit submission remains blocked.

### 5. Partial-fill semantics

The OMS must prove all of the following under tests:

- `PARTIALLY_FILLED` is non-terminal;
- cumulative filled quantity is monotonic and cannot exceed requested quantity;
- reconciliation updates the persisted cumulative fill exactly once;
- remaining reserved notional is based on unfilled quantity;
- duplicate venue fills are idempotent;
- cancel after partial fill does not erase the filled exposure;
- a venue position inconsistent with cumulative fills triggers drift and the central kill switch when critical.

### 6. Exchange accounting and equity

For Bybit TESTNET, portfolio/risk state must derive from venue reconciliation snapshots, including:

- wallet equity and available balance;
- positions by symbol/side;
- mark/entry price and unrealized PnL;
- fills and fees;
- open orders and remaining quantity.

PAPER balance must not be used as the source of truth for TESTNET risk decisions.

### 7. Bybit market-data feed

The TESTNET readiness path must validate a Bybit market-data source independently of the Binance staging feed:

- deterministic candle normalization;
- warm-up/history coverage;
- gap detection;
- reconnect/backoff;
- stale-data blocking;
- clock-quality enforcement.

A Binance feed may remain available for PAPER, but it cannot be the sole proof for a Bybit Futures TESTNET release.

### 8. Tests required before merge

At minimum, CI must cover:

1. protected Bybit entry payload;
2. missing SL/TP blocks before network write;
3. leverage configuration success/rejection/timeout;
4. reduce-only exit semantics;
5. partial fill -> reconciliation -> cancel lifecycle;
6. duplicate fills are idempotent;
7. restart preserves protection fields;
8. Bybit venue equity/positions replace PAPER accounting in TESTNET risk state;
9. Bybit market feed reconnect/gap/staleness behavior;
10. kill switch blocks queued submission after release revocation.

## Release sequence after implementation

1. CI green on the exact final SHA.
2. Disposable PostgreSQL migration validation.
3. PAPER hosted canary and restart/recovery validation.
4. Regenerate Month 11 / Month 12 evidence for the final SHA.
5. Obtain genuine independent external attestation for that exact evidence bundle.
6. Run the bounded local no-network canary.
7. Generate a short-lived `APPROVED_TESTNET` gate decision.
8. Configure Bybit TESTNET credentials server-side only.
9. Run the minimum remote TESTNET canary, then immediately reconcile and verify flat/expected state.

Any ambiguity activates the kill switch, cancels visible TESTNET orders where safe, reconciles until flat, returns the runtime to PAPER, and requires fresh evidence before another attempt.
