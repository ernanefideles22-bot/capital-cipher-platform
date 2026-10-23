# Railway staging: Supabase connection prepared, cutover blocked

Scope: existing `capital-cipher-platform` / `staging`, Supabase project
`phkligpkcitbbefrrotk`. This preparation does not deploy, migrate data, change
credentials, or change the current Railway connection.

## Evidence from 2026-10-23

- Backup `capital-cipher-dados-recuperados.tar.gz`, SHA-256
  `d08c8ca5c704804b6c0d64ab5379e27ed9b7e10b4c4b889dd4876843ab1da861`.
- PostgreSQL 17.5 embedded in PGlite 0.4.6 restored all 56 table schemas.
  All 43 selected table counts and canonical JSON SHA-256 hashes match the
  PostgreSQL 18 source: 76,247 rows. All 278 constraints are validated, including
  22 foreign keys and 171 CHECK constraints; 55 application tables have RLS.
  This validates SQL/data compatibility in the embedded engine. It does not
  validate native server operation, Supabase authentication or a hosted restore.
- Initial Supabase SQL requests timed out. An advisor request reported the
  project hibernated. After a public-key Auth health request returned 200,
  advisors and SQL responded again. This sequence is consistent with wake-up;
  it does not establish the cause of every timeout.
- Supabase SQL reports PostgreSQL 17.6, TLS active, database size 801,770,643
  bytes, and an existing least-privilege `capital_cipher_staging` role with
  connection limit 8. Existing destination data differs: 459 audit rows,
  164 decisions, zero specialist evidence. The recovered backup contains
  19,460 audit rows, 6,395 decisions and 23,088 specialist evidence rows.
- Destination has 54 private application tables; `oms_order_protections` is
  absent. The Railway migration tracking table is also absent. Destination
  schema and data were not changed.
- Existing backend `/health` returns 200 but `/ready` returns 503 with database
  false and broker true. `/api/v1/status` reports database CONNECTED based on
  the repository object's existence; it is not a SQL check. OMS returns a
  database connection error. The backend still runs the previous revision.
- Selected backend suite: 144 tests passed, including PAPER fills, stop/exit
  behavior, risk guards, security, health, recovery and the new connection
  boundary. A separate disposable copy of the restored PostgreSQL 17.5 database
  passed an application-repository integration test: decision/risk persistence,
  PAPER fill, recovery of the open position after restart, close, and recovery
  of the closed trade and balance after a second restart. No exchange network
  call was made. The embedded socket adapter requires a non-TLS loopback test
  connection and has a Terminate-handshake limitation; the test child exits
  before the parent closes the adapter. These are test-only conditions, not
  hosted connection settings.

## Implemented preparation

The current `.railway.internal` database rule stays the default. Only explicit
`STAGING_DATABASE_PROVIDER=supabase` enables the alternate gate. That gate pins
the destination project, exact expected hostname, database, least-privilege
role, port 5432 session mode, `sslmode=verify-full`, CA file and pool budget.
It rejects alternate DSN options, duplicate TLS options, privileged roles,
other projects and unverified TLS. Existing PAPER and TESTNET credential guards
remain active, and Redis remains pinned to its existing Railway private host.

Supabase's pooler hostname must be copied from its Connect dialog. Do not derive
the cluster index from the region. The current backend has IPv6 egress disabled;
prepare an IPv4 session-pooler destination without purchasing an IPv4 add-on.

`SUPABASE_CA_CERT_B64` must contain the public database CA obtained from the
project's Database settings. The launcher validates and writes it privately to
`/run/secrets/supabase-ca.crt`; no trust certificate is fetched automatically.
Do not set an admin or service-role database credential as the runtime login.

Prepared non-secret settings, **not applied**:

| Setting | Prepared value |
| --- | --- |
| `STAGING_DATABASE_PROVIDER` | `supabase` |
| `STAGING_EXPECTED_DATABASE_HOST` | Exact Session pooler host from Connect |
| `DATABASE_POOL_SIZE` | `4` |
| `DATABASE_MAX_OVERFLOW` | `0` |
| `AGENT_WORKER_MAX_CONCURRENCY` | `4` or lower |
| `SYSTEM_MODE` | `PAPER` |
| `OMS_EXECUTION_ENVIRONMENT` | `PAPER` |
| `OMS_TESTNET_ENABLED` | `false` |
| `OMS_TESTNET_ACKNOWLEDGEMENT` | Empty |
| `OMS_WORKER_ENABLED` | `false` |
| `OMS_RECONCILIATION_ENABLED` | `false` |
| Deploy health check | `/ready` |
| Predeploy after verified migration | `python -m scripts.verify_railway_supabase` |

`DATABASE_URL` must use `postgresql+asyncpg`, database `postgres`, role
`capital_cipher_staging.<project-ref>` through the copied session pooler host,
and exactly `sslmode=verify-full&sslrootcert=/run/secrets/supabase-ca.crt`.
Store the password only in the deployment secret store. Redis and data-lake
settings stay unchanged.

The existing predeploy `run_railway_migrations.py` only accepts Railway hosts
and changes runtime passwords. Do not reuse it for this Supabase destination.
The new verification command performs SELECTs, verifies the existing role,
membership, TLS and full application schema/RLS, and never applies migrations.
Limit the runtime to one backend replica until the combined connection budget
across all services has been measured.

## Gates before any connection change

1. Validate native PostgreSQL 17 restore and service behavior in an authorized
   environment with sufficient existing capacity. The embedded test alone
   cannot clear this gate.
2. Preserve and reconcile existing Supabase data and users; do not overwrite
   it with the recovery dump. Measure quota and live growth. The current Free
   database size already exceeds the advertised 500 MB database allowance;
   resolve capacity without deleting data or purchasing resources implicitly.
3. Reconcile missing schema and both migration histories without replaying
   arbitrary migrations over existing data. Verify all 43 manifests and all
   constraints on the proposed destination.
4. Supply the exact session-pooler host, verified CA and runtime credential
   through the approved deployment configuration; pass the read-only predeploy
   gate and isolated PAPER integration checks.
5. Only then change the backend connection, monitor `/ready`, test durable
   PAPER restart behavior, and reconcile other dependent services separately.

PostgresEU2's original volume must remain untouched. Its current maintenance
deployment does not start a database listener. No LIVE/TESTNET, resource
purchase, volume deletion or backend connection change is part of this patch.

References: https://supabase.com/docs/guides/database/connecting-to-postgres
and https://supabase.com/pricing .
