# Intelligence brain v1 (opt-in PAPER research)

The existing runtime has 300 agents (3 PRIMARY, 297 SHADOW). Its AgentInput,
AgentOutput, registry, orchestration, execution-scoped AgentMemoryEntry traces,
and SpecialistEvidence/AgentEvaluationService contracts remain unchanged.
The initial intelligence/reasoning.py deterministic baseline is preserved.

## New entry point

Import PaperBrain, EvidenceStore and EpisodicMemory from
app.agents.intelligence.brain. Construct process-local stores and optionally
inject an async ReasoningProvider with name, model and complete(request).
Call await brain.reason(episode_id, snapshot, as_of=aware_datetime,
horizon=timedelta(minutes=15)). No provider means deterministic baseline.

The provider receives a versioned request with the serialized snapshot,
untrusted external evidence and matching evidence_ids. Return an Assessment
JSON object: bias, summary, counter_evidence, invalidation, evidence_ids and
optional probability_up. action is always WAIT and both authority flags false.
Unknown fields/citations, invalid probabilities, oversized or malformed output,
provider exceptions and timeout produce a recorded deterministic fallback.
Cancellation propagates. Exception messages and raw responses are not saved.
No network adapter, secret lookup, SDK dependency or automatic invocation exists.
An adapter must implement cancellation-safe async I/O; blocking adapters cannot
be forcibly interrupted by asyncio. Provider metadata must be non-secret.

## External evidence and temporal rules

Ingest caller-supplied Evidence with source/event identity, provenance URI,
observed/received/expiry times and the SHA-256 of UTF-8 content. Ingestion verifies
the digest and rejects identity conflicts. Attribution does not verify truth.
Only matching symbol or GLOBAL evidence received by as_of and not expired is
eligible. At most 32 records are sent, sorted by ID for deterministic replay.
Evidence text is untrusted; instructions do not grant tools or authority.
The caller must supply a point-in-time snapshot already available at as_of;
the current snapshot contract has no separate receipt timestamp.

## Episodic evaluation

Episodes retain the complete input evidence, model identity, assessment and
fallback status. IDs cannot be reused by reason(); memory.append is idempotent
for identical records. Memory and outcomes are immutable through the public API.
Evaluation requires the exact target timestamp, matching symbol/timeframe,
finite positive price and an attributed receipt at or after observation.
Conflicting settlements are rejected. Historical recall excludes future episodes
and outcomes not yet received. Neutral hypotheses and flat returns have no
correctness score; Brier loss exists only when the model supplies probability.
These are observational scores, not P&L, calibration claims or trade approvals.

## Scope and limitations

Storage is process-local and not durable or bounded. No migration, production
DB access, runtime registration, ranking update, deployment or order execution
is added. This layer is explicitly invoked by research code; production wiring,
real-provider adapters, durable storage, source authentication and statistically
adequate evaluation remain future work. The model's probability is uncalibrated.

Run from backend: python -m pytest app/tests/test_intelligence_brain.py
app/tests/test_intelligence_reasoning.py -q (on one command line).
Tests use fake providers and block outbound socket connections. Windows asyncio
needs its internal loop sockets initialized before the test network guard.

## Validation (2026-10-15, Windows / Python 3.14.7)

- Intelligence plus runtime, specialist evaluation, 300-agent shadow cohort,
  orchestrator and contracts: **82 passed** (17 intelligence tests included).
- Full backend: **374 passed, 2 skipped, 3 failed**, one asyncio/thread warning.
- Original commit 637330a, same environment: **361 passed, 2 skipped, 2 failed**.
  Both durable-data-lake failures reproduce on the original code:
  test_worker_archives_raw_page_before_normalized_dataset and
  test_retryable_worker_result_is_rescheduled.
- test_protected_catalog_api_materializes_and_reads_manifest failed in the full
  changed suite but passed on the original suite and in isolation. Its cause
  remains unresolved; a fully green backend suite is not claimed.
- PostgreSQL and Redis integration tests skipped (no test services configured).
  Tests used SQLite in memory/local temporary files, fake LLM providers and no
  production configuration. Temporary paths were moved into the workspace
  after Windows denied access to the default pytest temporary directory.
- No existing source files were modified. No deploy or production operation ran.
