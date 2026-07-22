# Month 12 — independent audit and release readiness

Month 12 closes the implementation roadmap with a fail-closed release process.
It does not activate TESTNET, does not introduce LIVE, and does not treat an
automated self-check as an independent audit. The platform produces a
content-addressed evidence bundle; a reviewer independent of the development
team must attest that exact bundle before even a local TESTNET canary rehearsal
can pass.

## Delivered boundary

| Control | Result |
| --- | --- |
| Technical audit | Read-only, deterministic scan of the source tree and ten mandatory controls |
| Independent review | Strict external attestation contract, maximum validity of 30 days |
| Release gate | `APPROVED_TESTNET`, `BLOCKED_PENDING_EXTERNAL_AUDIT` or `BLOCKED_TECHNICAL` |
| Canary | One local, no-network virtual order, at most USD 100, one position and 1x leverage |
| Reversal | Kill switch, cancellation, flat reconciliation and rollback are mandatory |
| Persistence | Four private, append-only, RLS-enabled PostgreSQL tables |
| API | Authenticated read-only evidence; no submission or activation endpoint |
| LIVE | Absent from enums, adapters, configuration and release decisions |

## Evidence chain

```text
Exact source revision + passed Month 11 report
  -> automated technical evidence bundle
  -> external signed audit artifact
  -> local no-network TESTNET control rehearsal
  -> short-lived TESTNET-only release decision
  -> separate operator configuration outside the API
```

Every object contains its source or parent identity and a SHA-256 digest. A
mismatch, expired review, failed check, missing rollback, remote call attempt or
critical finding blocks the gate.

The software cannot generate an external approval for itself. In the current
repository state no real external attestation is present, so the correct formal
outcome is `BLOCKED_PENDING_EXTERNAL_AUDIT`. This is a successful safety result,
not an incomplete bypass.

## Ten mandatory audit checks

1. official CI quality gate;
2. exactly 56 versioned contracts;
3. all migrations on an empty disposable local PostgreSQL schema;
4. PAPER and TESTNET environment segregation;
5. absence of LIVE execution;
6. content-addressed, passed Month 11 shadow report;
7. private append-only release evidence;
8. central kill switch and guarded reset;
9. explicit `TESTNET_ONLY_NO_REAL_FUNDS` acknowledgement;
10. exact Binance/Bybit testnet host allowlist.

The audit runner is independent of the OMS control path and cannot submit,
cancel or reconcile an exchange order:

```bash
python scripts/run_release_readiness_audit.py \
  --source-revision <40-hex-commit> \
  --month11-report-id <immutable-report-id> \
  --month11-report-sha256 <64-hex-digest> \
  --ci-quality-gate-passed \
  --database-migrations-validated
```

The two boolean switches assert evidence produced outside the script. They do
not run CI or a database and must be supported by the corresponding artifacts.

## External attestation

An approval must identify an external reviewer and organization, reference the
exact evidence bundle hash and source revision, have zero unresolved critical
findings, reference a signed artifact digest and expire within 30 days. The gate
reduces that validity to at most 24 hours.

There is deliberately no HTTP endpoint for submitting attestations. Controlled
operator tooling must validate and persist a signed artifact through the
private backend database role. Browser roles (`anon` and `authenticated`) have
no table or function privileges.

## TESTNET canary rehearsal

The Month 12 harness is intentionally local and makes no network call. It proves
the release-control sequence:

1. an unapproved attempt is rejected;
2. an approved virtual canary is limited to one order, one position, USD 100 and
   1x leverage;
3. the kill switch is triggered immediately;
4. the canary is canceled;
5. reconciliation returns flat;
6. rollback completes.

This rehearsal is necessary for a release decision but does not change
`OMS_EXECUTION_ENVIRONMENT`. A real exchange TESTNET smoke test remains a
separate operator action using the Month 7 boundary, runtime-only sandbox
credentials, PostgreSQL and the exact acknowledgement. It can occur only after
a current `APPROVED_TESTNET` decision.

## Persistence

Migration
`20260720215457_create_release_readiness_attestations.sql` creates:

- `capital_cipher.release_evidence_bundles`;
- `capital_cipher.independent_audit_attestations`;
- `capital_cipher.testnet_canary_drill_reports`;
- `capital_cipher.release_gate_decisions`.

All four tables are RLS-enabled and have no policies or browser grants. A
`SECURITY INVOKER` trigger rejects update and delete, making evidence append-only.
The migration is versioned locally; this work does not modify hosted Supabase.

## Read-only administrative API

```text
GET /api/v1/operations/release-readiness/evidence
GET /api/v1/operations/release-readiness/attestations
GET /api/v1/operations/release-readiness/canary-drills
GET /api/v1/operations/release-readiness/gates
```

Every route requires the administrator key. There is no audit-submission,
canary-start, gate-evaluation, runtime-activation or LIVE endpoint.

## Release and rollback runbook

Before a TESTNET operator rehearsal:

1. pin the exact commit and verify a clean source tree;
2. retain official green CI evidence;
3. validate all migrations against an empty disposable local PostgreSQL schema;
4. retain the passed Month 11 report and its SHA-256 digest;
5. generate the technical evidence bundle;
6. obtain and independently verify the signed external attestation;
7. run the local no-network canary and require every control to pass;
8. generate a gate decision and verify its source, hashes and expiry;
9. configure TESTNET only on the server, never through the frontend;
10. use exchange sandbox credentials with no withdrawal permission.

If any check fails or the live TESTNET smoke test becomes ambiguous:

1. trigger the durable central kill switch;
2. stop the OMS and reconciliation workers;
3. cancel any visible TESTNET order through the existing idempotent boundary;
4. reconcile orders, fills, positions and balances until flat;
5. return `OMS_EXECUTION_ENVIRONMENT=PAPER`;
6. rotate affected sandbox credentials;
7. append incident and rollback evidence; never edit prior records;
8. require a new source revision, evidence bundle and external attestation.

No capital increase and no LIVE implementation is authorized by Month 12.

## Completion criteria

Month 12 implementation is complete when:

- the four contracts validate and the manifest contains 56 schemas;
- technical evidence is deterministic and fails closed;
- the service does not generate external attestations and exposes no HTTP
  submission route;
- the bounded canary and rollback sequence are tested;
- all release artifacts round-trip through immutable private storage;
- read APIs are authenticated and mutation routes do not exist;
- all migrations, backend tests and monorepo gates pass;
- the official PR CI is green;
- LIVE remains absent and the current release stays blocked until a genuine
  external attestation is supplied.
