# Staging PAPER — real-time shadow operations

This environment is the first operational phase after the twelve-month build.
It runs the production-shaped backend, PostgreSQL, Redis Streams, public market
data and all 300 registered agents, but the execution boundary remains PAPER.
There is no LIVE adapter, and TESTNET is explicitly rejected by staging
configuration validation.

The repository contains two deployment paths:

- `LOCAL_COMPOSE`: a loopback-only rehearsal using PostgreSQL 17 and Redis;
- `HOSTED`: the same backend connected server-side to a dedicated hosted
  Supabase/Postgres staging project and a TLS Redis service, using
  `deploy/staging/compose.hosted.yml`.

The Compose path does not automatically create, link or mutate a hosted
Supabase project.

## Provisioned hosted database

The dedicated Supabase project is `capital-cipher-staging`, reference
`phkligpkcitbbefrrotk`, in `sa-east-1`. It was provisioned on 2026-07-22 on
the Free plan and contains no production or legacy data.

The backend must not boot against this project until migration
`20260723001208_bootstrap_private_schema_and_runtime_role.sql` is present in
remote migration history. The project reference is not a credential; database
passwords, connection strings and API secrets must remain in the deployment
secret store.

## Enforced invariants

`APP_ENV=staging` refuses to boot unless all of the following are true:

- `SYSTEM_MODE=PAPER` and `OMS_EXECUTION_ENVIRONMENT=PAPER`;
- TESTNET enablement, acknowledgement, worker and reconciliation are disabled;
- no TESTNET credential is present in the process environment;
- PostgreSQL uses the async server-side driver;
- Redis is configured and `EVENT_BROKER_REQUIRED=1`;
- public market data, trusted clock, workers and operations monitoring are on;
- an administrator key of at least 32 characters is configured;
- leverage is fixed at 1x and CORS origins are explicit;
- the data-lake root is absolute;
- hosted PostgreSQL and Redis connections use TLS.

The staging entrypoint repeats the preflight immediately before replacing
itself with Uvicorn. Editing the Compose command cannot accidentally bypass the
Pydantic staging invariants.

## Local Compose rehearsal

Prerequisites are Docker Engine with Compose v2 and outbound HTTPS access for
the backend market-data connection. From the repository root:

```powershell
Copy-Item deploy/staging/.env.example deploy/staging/.env
```

Replace the three placeholders in `.env` with independent URL-safe random
values of at least 32 characters. Never add exchange keys. Then validate and
start the stack:

```powershell
docker compose --env-file deploy/staging/.env -f deploy/staging/compose.yml config --quiet
docker compose --env-file deploy/staging/.env -f deploy/staging/compose.yml up -d --build
docker compose --env-file deploy/staging/.env -f deploy/staging/compose.yml ps
```

The API is published only on `127.0.0.1`. PostgreSQL and Redis have no host
ports. The backend receives a separate egress network for public market data;
the data services remain on an internal network.

On the first creation of the PostgreSQL volume, the official image applies all
ordered files from `supabase/migrations/` through
`/docker-entrypoint-initdb.d`. The database must be fresh. Do not assume that
adding a new migration later will update an existing volume.

Do not run `down -v` against evidence you intend to preserve. A volume removal
is a destructive reset, not a routine restart.

## Runtime verification

The shallow liveness endpoint is `/health`. The deep `/ready` endpoint returns
HTTP 200 only when all required conditions hold:

- database and Redis respond;
- the state machine and OMS are PAPER;
- exactly 3 PRIMARY and 297 SHADOW agents are registered, all PAPER;
- the operations monitor exists and the market feed is connected;
- the durable kill switch is clear.

Example checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/ready
Invoke-RestMethod http://127.0.0.1:8000/api/v1/status
$headers = @{ 'X-API-Key' = '<ADMIN_API_KEY>' }
Invoke-RestMethod -Headers $headers http://127.0.0.1:8000/api/v1/operations/status
```

The watchdog checks these three surfaces every 30 seconds. Three consecutive
violations terminate the watchdog with a non-zero exit code. Its JSON logs
contain stable violation codes and never contain the administrator key.

## Hosted Supabase staging

Use a dedicated staging project or database branch, never the future production
database. Confirm its PostgreSQL major version before setting
`supabase/config.toml`; this repository now validates on PostgreSQL 17.

Required operator actions:

1. protect the Supabase and GitHub accounts with MFA;
2. enable database SSL enforcement and network restrictions;
3. apply migrations through a reviewed CI integration or an explicitly linked
   Supabase CLI workflow, not an ad-hoc browser paste;
4. run Security Advisor and Performance Advisor after migration;
5. use a direct or session-pooled server connection appropriate for persistent
   workers and include `sslmode=verify-full` where supported;
6. create a dedicated LOGIN role, grant it membership only in
   `capital_cipher_runtime`, and never connect the backend as `postgres` or a
   Supabase administration role;
7. keep the connection string only in the backend secret store;
8. keep the `capital_cipher` schema outside the Data API and preserve its RLS,
   revocations and append-only triggers;
9. use `rediss://` for the external Redis broker;
10. set `STAGING_DEPLOYMENT_TARGET=HOSTED` and run
   `python scripts/validate_staging_paper.py` before boot.

No `service_role` or database credential belongs in the frontend.

### Hosted deployment contract

The hosted Compose file runs only `preflight`, `backend` and `watchdog`.
PostgreSQL and Redis are external dependencies: it never creates a second
database or an unencrypted Redis instance. The API remains bound to loopback;
remote access requires an authenticated HTTPS reverse proxy supplied by the
chosen host.

Do not put real values in `deploy/staging/.env.hosted.example`. Inject
`DATABASE_URL`, `REDIS_URL` and `ADMIN_API_KEY` through the deployment
provider's secret manager. Passwords inside URLs must be URL-encoded.

The database connection must satisfy all of these conditions:

- it targets project `phkligpkcitbbefrrotk`;
- it authenticates as a dedicated custom LOGIN role that inherits only
  `capital_cipher_runtime`;
- it uses port 5432: direct IPv6 when supported, otherwise Supavisor session
  mode for an IPv4-only persistent host;
- it uses `sslmode=verify-full` with
  `sslrootcert=/run/secrets/supabase-ca.crt`;
- the project CA downloaded from the Supabase dashboard is mounted read-only
  from `SUPABASE_CA_CERT_HOST_PATH`, never fetched during container startup;
- its SQLAlchemy pool is bounded to five connections with no overflow by
  default.

The Redis connection must use `rediss://`, contain a strong runtime credential
and exactly match `STAGING_EXPECTED_REDIS_HOST`. Use a persistent service that
supports Redis Streams; a cache-only instance is not sufficient for the event
broker.

Before the first boot, validate without starting dependencies:

```powershell
docker compose --env-file <secret-env-path> -f deploy/staging/compose.hosted.yml config --quiet
docker compose --env-file <secret-env-path> -f deploy/staging/compose.hosted.yml build preflight
docker compose --env-file <secret-env-path> -f deploy/staging/compose.hosted.yml run --rm --no-deps preflight
```

Only after those commands pass may the operator start `backend` and `watchdog`.
The backend then verifies the complete migration-owned schema and RLS before it
becomes ready. Snapshot the `hosted-data-lake` volume independently; a named
volume is persistent on one host but is not, by itself, an off-host backup.

Current connection guidance:

- [Supabase database connection modes](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [Supabase SSL enforcement](https://supabase.com/docs/guides/platform/ssl-enforcement)

## Real wall-clock campaign

The live public Binance feed causes each closed candle to pass through clock
validation, raw persistence, normalization, all 300 agents, consensus, central
risk and PAPER execution. The 297 SHADOW agents have no order authority.

Run the campaign for at least 60 consecutive days, targeting 90 days before an
external audit. Preserve immutable daily evidence for:

- uptime and dependency-recovery mode;
- received, rejected, duplicate and gap-backfilled candles;
- executions, failures, timeouts and p95 latency per cohort;
- SLO evaluations, active/resolved alerts and cost budget;
- agent scorecards, drift observations and marginal contribution;
- PAPER orders, fills, positions, drawdown and reconciliation;
- code revision, configuration hash and database migration set.

Minimum acceptance thresholds:

- no LIVE or TESTNET execution attempt;
- zero orphan orders/fills and zero unreconciled critical mismatch;
- no loss of append-only audit evidence;
- every critical dependency failure reaches `SAFE_HALT`;
- shadow work stops in `DEGRADED` or budget `HARD_LIMIT` states;
- recovery follows the configured consecutive-success gate;
- no unresolved security-critical alert;
- at least 60 days of real elapsed evidence, not accelerated replay.

## Incident and rollback

If readiness or watchdog checks fail:

1. keep the environment PAPER and do not add TESTNET credentials;
2. inspect `docker compose logs backend watchdog` and the operations alerts;
3. if database, audit or central risk is unhealthy, keep `SAFE_HALT` latched;
4. stop only the backend/watchdog while preserving PostgreSQL, Redis and the
   data-lake volumes;
5. fix forward on a reviewed commit and restart the same PAPER boundary;
6. start a new evidence window if continuity or integrity was lost.

Passing staging is evidence for an independent audit. It is not authorization
for TESTNET or LIVE capital.
