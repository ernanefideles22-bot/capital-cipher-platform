# Fly.io hosted PAPER staging

This directory binds the provider-neutral hosted staging contract to Fly.io.
It does not authorize TESTNET or LIVE and contains no credentials.

## Fixed topology

- Region: `gru` (Sao Paulo).
- `backend`: one continuously running `shared-cpu-4x` Machine with 4 GB RAM.
- `watchdog`: one continuously running `shared-cpu-1x` Machine with 256 MB RAM.
- Database pool: 3 persistent connections, leaving recovery and rolling-deploy
  headroom inside the staging role's 8-connection limit.
- Data lake: encrypted `hosted_data_lake` volume mounted only by `backend`,
  initialized at 20 GB with 14-day snapshot retention.
- Ingress: HTTPS terminates at Fly Proxy and routes only to `backend`.
- Preflight: every release runs `validate_staging_paper.py`; backend startup repeats
  the same validation before Uvicorn starts.

Both process groups have autostop disabled. The watchdog probes the public HTTPS
origin so it cannot accidentally resolve itself through an app-wide `.internal`
address.

## Secret contract

Create these with `fly secrets set`; never place their values in this repository:

- `DATABASE_URL`: custom Supabase LOGIN role, port 5432, `sslmode=verify-full`,
  and `sslrootcert=/run/secrets/supabase-ca.crt`.
- `REDIS_URL`: the Fly-provisioned, organization-private Upstash URI using
  `redis://`. It is accepted only together with
  `STAGING_REDIS_PRIVATE_NETWORK=FLY_6PN`, an exact pinned
  `fly-*.upstash.io` host, and port 6379. Other hosted Redis connections must
  use `rediss://`.
- `STAGING_EXPECTED_REDIS_HOST`: exact host present in `REDIS_URL`.
- `ADMIN_API_KEY`: at least 32 random URL-safe characters.
- `SUPABASE_CA_CERT_B64`: base64-encoded Supabase database CA certificate.

Fly decodes `SUPABASE_CA_CERT_B64` into the read-only guest file declared in
`fly.toml`. Do not use the Supabase `postgres` account or exchange credentials.

## Provisioning sequence

Use Fly CLI commands from the repository root and always pass
`--config deploy/fly/fly.toml` where supported.

1. Validate the file with `fly config validate`.
2. Create the app without deploying it.
3. Allocate a 20 GB `hosted_data_lake` volume in `gru`.
4. Allocate a stable app-scoped outbound IPv4 address before applying Supabase
   network restrictions.
5. Create Upstash Redis in `gru`, persistence enabled and eviction disabled.
   Use its organization-private IPv6 endpoint over Fly's WireGuard 6PN.
6. Create the least-privilege Supabase LOGIN role and load all five Fly secrets.
7. Deploy; the release command must pass before either process group changes.
8. Confirm `/ready`, `/api/v1/status`, the authenticated operations status,
   process counts, mounted volume, Redis Streams, database grants, and watchdog.

If any check fails, keep or scale both process groups to zero and rotate any
credential that may have been exposed. Never bypass the preflight to recover a
deployment.
