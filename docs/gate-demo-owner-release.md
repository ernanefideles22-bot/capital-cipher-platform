# Gate demo release by the project owner

This is a separate release policy for Gate Testnet virtual funds, requested by
the owner on 2026-10-30 because the project has no external reviewer. It neither
creates an independent audit nor changes the existing external-audit release
contracts or the main PAPER application. The dedicated Gate composition root is
the only caller. There is no new public mutation route or production endpoint.

## Measured evidence before activation

Run the corrected shadow replay from `backend/`:

```bash
python -B scripts/run_month11_shadow_validation.py > gate-shadow.json
```

It disables external collectors, ignores local dotenv configuration, uses a
Gate-labeled synthetic dataset and passes that exchange through the agent
runtime. A CLI audit hook rejects DNS and IP socket operations. Acceptance
requires PASSED, zero network attempts, 300 agents, 1,800 executions and zero
orders. Missing external inputs stay WAIT/MISSING_EVIDENCE; no data is invented.
Seven replay days are synthetic, not seven elapsed days or profit evidence.

Retain this actual report and its hash together with green CI for the exact
revision, migration validation, and Gate protection tests. Hash a canonical
manifest of those real artifacts to obtain `technical_evidence_sha256`. This is
an operator-supplied evidence reference, not a runtime claim that CI ran on the
server. Never use a fixture digest or an invented successful report.

## Two explicit stages

1. **CANARY_ONLY:** A server-side owner grant authorizes one centrally approved
   operation. The default `canary_trigger=AGENT_SIGNAL` waits for an actual
   agent BUY/SELL. An explicit `canary_trigger=TECHNICAL_PROBE` instead schedules
   one mechanical BUY from the executor tick, without waiting for or changing
   an agent signal. BTCUSDT / 15m only, MARKET IOC, at most
   50 USDT, one position, isolated 1x and the existing 0.1% central risk cap.
   Agent HOLD/WAIT never becomes a trade signal. Technical probes use the
   distinct strategy `GATE_TECHNICAL_CANARY`, reason and warning fields, and
   `strategy_performance_sample=false` in the measured report. This is execution
   evidence only; its PnL is not a profitability sample for agent strategies.
   The same actual central risk check can reject either path. The order ID is reserved durably
   before the dispatcher can send it. Another queue command cannot use that
   reservation. Restart, expiry or a new grant ID do not replenish the canary
   budget for the same revision and account.
2. The adapter must confirm entry and an owned open stop. The monitor takes
   fresh signed entry/position/stop reads, stores their identities and timestamps,
   then closes the canary reduce-only while protection remains in place. Only
   after confirmed exit, canceled protection, fresh complete flat inventory and
   central reconciliation can it append a VERIFIED canary record.
3. It **stops at AWAITING_OWNER_CANARY_ACCEPTANCE**. No-fill, stop failure,
   ambiguous writes, missing evidence or foreign exposure cannot produce a
   successful canary report. Existing HALTED and kill state are never reset.
4. **AGENTS:** After the owner reviews the actual evidence, a separate grant
   must reference its exact SHA-256. The stored report must match the same
   revision, account and technical-evidence manifest. An arbitrary hash, a
   simulated test result or a report from another revision cannot enable agents.

Every grant has a unique identity, owner ID, authorization reference, exact
40-character source revision, evidence digest, virtual-funds acknowledgement,
issue time and expiry (at most 24 hours). Invalid configuration prevents startup;
missing configuration preserves the original external-audit policy. An expired
entry grant never prevents an owned, risk-reducing safety exit.

## Operator preparation

This command only prints validated JSON for review; it never sends an order:

```bash
python scripts/prepare_gate_demo_grant.py \
  --source-revision <exact-commit-sha> \
  --technical-evidence-sha256 <actual-manifest-digest> \
  --owner-id ernanefideles22-bot \
  --authorization-reference '<owner authorization reference>' \
  --mode CANARY_ONLY --hours 1
```

For the separately identified mechanical test, add
`--canary-trigger TECHNICAL_PROBE` to this preparation command. The proposed
server grant must explicitly contain that trigger. No public write endpoint or
research payload can select it; research cannot claim the reserved technical
strategy. Existing grants without the field keep their original behavior and
immutable stored representation. `AGENTS` grants reject the technical trigger.

The mechanical key is account/revision scoped and the monitor serializes it
under the existing flow lock. An evaluated rejection or interrupted queue step
becomes `TECHNICAL_CANARY_REVIEW_REQUIRED`; it never issues a fresh risk identity
to retry the same test. Writes-off, expired authorization, kill state, lease
loss, stale market data, pending recovery and the existing one-canary budget
still block it. The protected close and subsequent owner acceptance are
unchanged. A risk-reducing exit remains possible after grant expiry.

Publish the exact tested revision on the existing service with writes **off**.
The owner-controlled Railway configuration then supplies:

- `GATE_DEMO_OWNER_GRANT_JSON`: the reviewed CANARY_ONLY JSON;
- `GATE_OMS_ACKNOWLEDGEMENT=GATE_TESTNET_VIRTUAL_FUNDS_ONLY`;
- `GATE_OMS_WRITES_ENABLED=true` only for the bounded canary stage.

Server configuration is the trusted operator channel; this JSON is not a
cryptographic signature or independent attestation. Do not put it in the frontend
or use it to authorize real funds. Credentials remain the existing testnet-only
key and secret. No additional paid resource or database volume is needed.

The public status exposes only mode, expiry validity, reservation status,
canary digest and acceptance, never raw private account responses. The private
report can be exported in the existing service's console with read-only SQLite:

```bash
python scripts/export_gate_demo_canary.py --sha256 <observed-canary-digest>
```

For AGENTS, prepare a **new** reviewed owner grant with `--mode AGENTS` and
`--accepted-canary-sha256 <observed-canary-digest>`. Preserve the same revision
and technical evidence digest. Do not fabricate an acceptance hash. A grant for
AGENTS without a matching measured record is denied.

## Failure and rollback

Keep the existing volume, append-only release records, journal and risk history.
Stop failed or ambiguous validation for operator reconciliation. A no-fill
consumes the one canary attempt and does not count as a stop/exit proof. A restart
may collect missing terminal reads of the already-owned close, but cannot send a
replacement entry. All central risk, account lease, fresh inventory, fixed
testnet host, stop and exit checks remain mandatory.

Do not turn writes off while an owned position still needs a risk-reducing exit.
First use the existing protected close and reconcile flat; then disable writes.
Returning to read-only mode does not alter the main PAPER service or erase a
halt. Owner grants and measured observations are append-only on the existing
account-bound durable journal.
