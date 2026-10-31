# Gate Testnet stop expiration HTTP 400 — bounded candidate

## Observed failure (not inferred)

On the staging deployment e876b775-044f-40d4-9d4f-92df0cd83195 (source f432517b83ff6aadbe5ca4c40b0e351f1230904a), the next bounded canary ed0f1733-12da-5203-80a6-82b3f50ac9b3 was queued at 2026-10-31T03:04:23Z. At 03:04:43Z the transport rejected the protective stop with HTTP 400, code `AUTO_INVALID_PARAM_TRIGGER_EXPIRATION`, schema fields (`trigger`, `expiration`). The risk engine activated its kill switch. Later read-only inventory logs showed 0 positions, 0 open orders and 0 conditional orders; this is **not** evidence of an accepted stop or of completed OMS recovery.

## Candidate correction (not venue-proven)

The Gate Futures API v4 official `POST /futures/{settle}/price_orders` example uses `trigger.expiration=86400` (seconds). Previously this adapter submitted 3600. The public schema describes the value in seconds but does not explicitly publish the Testnet-accepted range or prove that 86400 is accepted for this close-all stop. For the next controlled validation only, build 86400 and reject all other values, including 3600, locally.

Keep all existing invariants: a finite duration, isolated 1x, one-way close-all size 0, `close=true`, `reduce_only=true`, `tif=ioc`, mark-price reference, correct adverse trigger, no retries or fallback payloads. A 24h expiry is not perpetual protection: stop expiry monitoring and immediate fail-closed exit/reconciliation remain mandatory.

## Rollout gate

Do **not** silently retry the failed order, requeue a consumed canary, reset a kill switch or enable AGENTS/LIVE. First reconcile the failed cycle with independently verified exchange fills and flat inventory, preserving all incident evidence. Obtain a new owner-approved one-attempt Testnet grant bound to the exact tested source. Only then deploy and observe a newly accepted `OPEN` stop read back by ID, verified closing behavior, fresh flat inventory and `MATCHED` OMS reconciliation.

Successful CI or staging deployment proves code acceptance, **not** exchange acceptance. If 86400 also fails, retain fail-closed state and capture its bounded error code, without guessing further expiration values.

Reference: https://www.gate.com/docs/developers/apiv4/en/futures/
