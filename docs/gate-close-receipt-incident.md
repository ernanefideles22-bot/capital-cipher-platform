# Gate Testnet close receipt and stop diagnostics

Source a99af699e83516f7af9f264be0d1ef418be9848a reached SUCCESS in Railway
deployment 373154ea-a221-4b09-bee0-bcc855862f4d. Its numeric protection read
worked. Canary c7d9d947-829c-52f7-90f6-a3eba3ad9274 dispatched once.

Signed exchange reads confirmed entry 82472172996632516 filled +3 BTC_USDT
contracts and emergency reduce-only exit 82472172996632517 filled -3.
The exit was is_close=true, is_reduce_only=true, is_liq=false, left=0,
status=finished, finish_as=filled. Complete current positions, open orders
and open stops were empty. Gate normalized the size=0 close-all request to
executed response size=-3. The previous validator required response size=0.

The corrected validator accepts a nonzero executed close size only when it
exactly negates independently persisted pre-close exposure. All identity,
close/reduce-only flags, terminal status, freshness, flat position and complete
inventory checks remain. Every adapter/journal/canary call site supplies
previously verified exposure; the close cannot supply its own expected size.
The shared simulated venue now returns normalized signed fills, exercising
the complete risk/OMS/protected-close/canary path. Legacy zero-size receipts
remain supported.

The stop failed at STOP_SUBMITTING before acknowledgement. No open or finished
price order was returned by the subsequent bounded history read. The previous
transport discarded HTTP status and error content, so the original stop failure
cause is NOT established. This change makes no parameter guess or successful
stop claim. Diagnostics now retain only HTTP status and an allowlisted public
error enum, and log the stage before emergency close changes it. Provider
messages, bodies, credentials, signatures and auth headers are never logged.
Timeouts, rejection responses and malformed replies still cause ambiguity and
are never retried.

The journal remains HALTED; OMS remains UNKNOWN version 2; the command remains
COMPLETED, one attempt; durable risk remains active at revision 3. This commit
does not clear risk, close the halted cycle, rewrite old events, requeue orders,
renew consumed budgets, certify protection or enable agents. Entry and exit
are incident evidence, not a successful protected canary or profit sample.

Before another canary, repeat signed account/order/fill/history reads, verify
the bound entry and exit against persisted exposure, append a reviewed emergency
recovery disposition without claiming a stop, preserve every old event and
reservation, and review the risk reset separately. Do not reuse the previous
prewrite-only recovery procedure: this order actually filled. A new canary needs
a tested exact source and current grant.

Official reference: https://www.gate.com/docs/developers/apiv4/en/futures/
