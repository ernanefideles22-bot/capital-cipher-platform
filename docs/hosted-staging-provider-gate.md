# Hosted staging provider gate

This gate must be completed before Capital Cipher creates a hosted backend,
Redis database or database LOGIN credential. It deliberately separates the
provider-neutral deployment contract from a purchase decision.

## Mandatory capabilities

The selected container host must provide:

- one continuously running Docker workload for at least 60 days;
- a persistent encrypted volume mounted at
  `/var/lib/capital-cipher/data-lake` with off-host snapshots;
- a provider secret manager and redacted deployment logs;
- HTTPS ingress while the backend container remains private or loopback-only;
- outbound TLS to Supabase, Redis and public market-data endpoints;
- read-only secret-file mounting for the Supabase database CA certificate;
- a stable outbound IP if Supabase network restrictions are enabled;
- restart policies, health checks, logs, resource limits and cost alerts.

The selected Redis-compatible service must provide:

- TLS on every connection and a strong rotatable credential;
- Redis Streams commands used by the runtime;
- persistence suitable for a broker, with a documented recovery objective;
- usage and command metrics, hard or alerted cost limits and a nearby region;
- no eviction policy that can silently discard Capital Cipher stream keys.

## Rejection conditions

Reject a provider or plan if any of these are true:

- workloads sleep or scale to zero during the evidence campaign;
- the filesystem is ephemeral and no persistent volume can be attached;
- Redis is cache-only, unencrypted or lacks Streams support;
- secrets must be committed to Git or embedded in an image;
- the estimated monthly maximum is not explicitly approved;
- the host forces a serverless lifecycle incompatible with the persistent
  market feed, agent workers and watchdog.

## Provisioning order

1. Obtain explicit approval for provider, region and maximum monthly cost.
2. Create the container service with backend execution disabled.
3. Attach and snapshot the data-lake volume.
4. Create the TLS Redis service and record its host pin in the secret manager.
5. Generate the administrator key in the secret manager.
6. Generate a database password in the same secret manager, create the custom
   Supabase LOGIN role and grant only `capital_cipher_runtime` membership.
7. Download the project database CA from Supabase and mount it read-only at
   `/run/secrets/supabase-ca.crt`.
8. Run the hosted preflight without dependencies.
9. Test database and Redis connectivity, schema verification and deep
   readiness while still in PAPER.
10. Enable the watchdog and begin a new evidence window.

No step in this gate authorizes TESTNET or LIVE execution.
