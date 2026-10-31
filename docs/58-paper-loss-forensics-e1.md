# Capital Cipher — Phase E1: investigate losses before modifying trading agents

**Status:** read-only forensics, stacked on research PR #66 (plus #60–#65).
**No deployments, no optimizer, no changes to strategies/risk/OMS, no
Gate Testnet or LIVE activity.**

## What is built

`app.strategy.paper_forensics_e1`:
- Conservative, immutable PAPER observations backed by order, matching
  decision, risk check, audit closure and agent output snapshots.
- Calculates realized net P&L, win rate and profit factor by strategy,
  symbol, timeframe and side; also planned stop-distance risk and
  **net** realized R-multiple. Open trades are excluded from realized P&L.
- Flags stop/target wrong-side orders, missing brackets, reward/risk <1,
  opposing simultaneous BUY+SELL on a shared symbol, mismatched decision,
  unavailable risk approvals, missing closing audit, and the anomalous
  TAKE_PROFIT event with net loss.
- Joins agent **opinions** only when from the same correlation and before
  the position opened. Disagreement does NOT mean an agent caused the
  subsequent P&L. The rank is limited to aggregate *strategy* metrics.
  Ranking samples <30 closes are marked inadequate.
- Never subtracts fees or slippage a second time. Slippage can already be
  embedded in simulated fill prices, and estimated cost fields may be
  informational only. True per-component P&L is NOT reconstructed from
  the relational `paper_orders` table alone.
- Detects incomplete audit trails; does not invent ATR, volume, candle
  patterns, future market context or agent/decision profitability.

`backend/scripts/paper_e1_readonly_report.py`:
- Authorized diagnostic command with **PostgreSQL only**, explicit
  `SET TRANSACTION READ ONLY`, 20-second statement timeout, single
  DB connection, 30-day default lookback, 250-order default bounded sample,
  capped agent-output read, and no schema mutations.
- Reads `capital_cipher.paper_orders`, `decisions`, `risk_checks`,
  `agent_outputs` and `audit_logs`. Joins order→decision/risk by exact ID,
  verifies correlation/market and joins audit closures by entity ID.
  Open orders and missing audit evidence are labeled separately.
- Outputs a JSON report with data gaps and truncation flags. Never
  overwrites an existing file; credentials are not printed.
- No DB credentials are supplied to GitHub Actions; its tests run without
  production data. There has been **NO current production DB E1 extract**.
  The earlier PAPER audit (12 closed/11 stops, net ~-784.50) is an
  historical baseline, not data fabricated by these E1 tests.

Example (only with authorized **read-only database credentials**, in an
isolated secure environment with backend + asyncpg installed):
```bash
PYTHONPATH=backend python backend/scripts/paper_e1_readonly_report.py \
  --lookback-days 30 \
  --order-limit 250 \
  --out /tmp/capital-cipher-e1-readonly.json
```

## Hard boundaries and data reliability

1. If the role cannot SELECT any required table due to RLS/privileges,
   stop and correct *reader permissions*, do not bypass protections.
2. A missing audit row cannot be treated as a proven absent stop, because
   retention/archive separation may hide historical evidence.
3. PAPER order table has no direct `exit_price` or `exit_reason`;
   they come from verified `PAPER_ORDER_CLOSED` audit payloads.
4. Opposing symbol/timeframe positions are flagged as possible one-way
   execution mismatch, *not* proof of actual Gate account position mode.
5. A correlation ID links observations temporally. It is **not a causal
   attribution** between an individual agent and a trade.
6. Forensic flags describe anomalies needing investigation. They cannot
   establish whether an entry was late, against trend or overpriced without
   independently collected point-in-time OHLCV/bid-ask and full decision
   time. No retrospective future candle is allowed as an entry feature.
7. This phase deliberately **does not remove or reweight agents**. Proposed
   remedies must be separately tested against an unchanged, frozen baseline
   with no lookahead and cost stress.
8. No standalone profitable strategy has been demonstrated in existing
   E1/D0–D2.4 evidence. 12 historic trades is far too few to rank agents.

## E2: what happens after an E1 real-data export

1. Confirm the PnL reconciliation against full PAPER audit and archived
   retention history. Preserve raw evidence and order IDs.
2. Segment the losses by failure modes: invalid structural stops, excessive
   costs, long/short conflict, trade-side disagreement, missing evidence.
   Separate **execution fidelity** from **strategy prediction quality**.
3. Replay the *same* decisions against historic quotes with costs and
   identify counterfactual improvements only on training/validation
   splits, not the sealed final holdout.
4. Compare unchanged baseline vs bounded proposed filters (market regime,
   volume confirmation, ATR stop validity) using preregistered trials,
   independent forward holdout and sufficient samples.
5. Keep unproven strategies SHADOW; any later PAPER promotion requires
   explicit human authorization through the existing risk/OMS.

**This PR is diagnostic only; no agent is automatically penalized or promoted.**
