# Platform role and migration policy

## Current role

`capital-cipher-platform` is the active implementation repository for the
Capital Cipher platform. It is deliberately **paper-only**: no live trading,
private exchange credentials, or production execution may be added without a
separate architecture, security, and governance review.

The related repositories have distinct roles:

| Repository | Role |
| --- | --- |
| `capital-cipher-specification` | Authoritative architecture, decisions, and versioned contracts. |
| `capital-cipher-platform` | Active implementation of the paper-trading platform. |
| `capital-cipher-ai` | Legacy prototype and a source for selectively migrating dashboard UX. It is not an approved execution path. |

## Rules for changes

1. Implement new capabilities here only after their contract or architectural
   decision is captured in `capital-cipher-specification`.
2. Preserve the paper-only boundary. A feature must fail closed if it is asked
   to execute outside the permitted environment.
3. Treat risk controls, audit events, reconciliation, and idempotency as
   requirements, not optional agent features.
4. Keep CI green before merging. Every behavior change needs automated tests;
   critical risk and execution paths need negative tests as well.
5. Public deployment requires authentication, role-based authorization, rate
   limiting, secret management, observability, and a threat-model review. They
   are not yet provided by this Phase 1 repository.

## Legacy dashboard migration

Only reusable presentation components and non-trading user-experience ideas
may move from `capital-cipher-ai`. Do not port its direct exchange execution,
client-writable operational state, simulated backtest results, or autonomous
browser runtime.

Migrate the dashboard in small, reviewable slices:

1. Define the API contract in the specification.
2. Implement the backend endpoint with authorization, validation, tests, and
   audit events.
3. Add the frontend view, loading/error states, and browser-level tests.
4. Validate the complete paper-trading workflow in an isolated environment.

## Archival gate for the legacy repository

Do not archive or delete `capital-cipher-ai` until all of the following are
true:

- its useful UI has been migrated and accepted;
- every deployed reference to its Supabase functions has been removed;
- any exposed or historical credentials have been reviewed and rotated;
- the legacy mainnet execution routes are disabled and independently verified;
- the specification records the migration decision and repository disposition.

Archival is an administrative action to take after this gate, not a substitute
for disabling unsafe deployed services.
