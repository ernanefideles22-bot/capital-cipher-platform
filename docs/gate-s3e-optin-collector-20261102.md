# S3E — opt-in durable public-observation collector, prepared without deployment

Repository-only integration on top of PR #81 (`7ae8609`). No merge, Railway
change, variable change, service creation, volume operation or order. This
branch prepares collection; it does not establish hosted S3 observations,
availability or profitability. Release still requires reviewing the S1 audit
and sequential S2E/S3 acceptance. Existing prepared S2E release is separate.

## Activation and data boundary

- Default is OFF. All three flags must be exactly `true`:
  `GATE_S1_SHADOW_ENABLED`, `GATE_S2E_SHADOW_ENABLED`,
  `GATE_S3_SHADOW_ENABLED`. S3 alone or any incomplete combination creates no
  S3 worker, directory or journal. No such flags were changed on Railway.
- `professional_shadow_snapshot` can pass a deep copy of its newest closed
  15m candle and validated S2E hypothesis summary, with the **original actual
  observation cutoff**, to the optional capture callback. No additional
  exchange fetch or private request is made by this callback or collector.
- S1 and S2E reports keep their own statuses when capture/storage fails.
  S3 never receives the service's environment, credentials, OMS runtime,
  account balances, registry or 300-agent decision object.
- Only the existing S3 field allowlist is serialized before queuing. No raw
  candle or arbitrary callback response is added to public S1 status.

## Isolated persistence and worker

One daemon worker owns the SQLite connection and every disk operation. The
event loop enqueues without waiting for disk into a **12-item bounded queue**,
with at most **32 KiB per serialized public observation**. API queries read a
copy of a cached verified comparison, not SQLite.

S3 requires the existing persistent root to have passed mount verification.
Its distinct directory/file are:

```text
<persistent-root>/capital-cipher-gate-s3/forward-v1.sqlite3
```

It does not use the legacy `capital-cipher-gate-testnet/research.sqlite3` or
`cycles.sqlite3`, Postgres or Redis. A new directory is mode 0700 and a new
journal mode 0600. Reject symlinked roots/directories/files and file hardlinks.
An existing file must already identify as S3 before the journal constructor
can initialize anything. Corrupt evidence is blocked, never reset/deleted.

A POSIX nonblocking advisory `writer.lock` excludes a second collector;
unsupported platforms block only the optional worker. Ordinary S3 hash/head
and deterministic replay checks remain in force. This is not protection from
an administrator rewriting the complete database or ignoring advisory locks.

Before startup/writes, stop S3 if available disk space falls below **128 MiB**.
This is an advisory guard, not a physical reservation against other writers.
The worker admits at most **10,000 observations per journal** and then requires
explicit review; it never rotates, deletes, truncates or silently starts a new
window. Longer histories, storage sizing and replay performance need separate
runtime validation. The underlying offline reader retains its existing 100,000
event replay limit; neither limit is a proven production latency guarantee.

## Receipt, failure and restart semantics

`QUEUED_NOT_COMMITTED` is deliberately distinct from a persisted observation.
Only committed rows survive a process failure. On startup the original scorer
replays and verifies committed evidence, restoring open hypothetical positions,
cost assumptions and completed results; the comparison cache is reconstructed.
No missed historical candle is supplied with an invented real-time receipt.

If the next public snapshot repeats a candle with unchanged OHLCV, retain the
**first** observation, receipt and decision. A later hypothesis does not replace
it. Changed OHLCV, backwards clocks, gaps, invalid scope, queue overflow, disk
failure or report verification failure block S3 and clear cached financials.
After a successful commit but failed report calculation, report BLOCKED with
the durable count retained; restart can recover that committed event.
Exceptions are represented by fixed error codes, without private error strings.

When S3 is opted in, sampling is anchored to 15m boundaries plus 10 seconds,
instead of accumulating HTTP/analysis latency each cycle. S1's default 900s
pause is unchanged when S3 is off. Provider delays, process outages or other
gaps can still block collection; this is not a completeness guarantee.

Shutdown stops admission, drains pending events and closes on the owning
thread. Join waits at most 5 seconds; outstanding I/O is explicitly
`SHUTDOWN_PENDING` and may still commit afterward. Hard process exit may lose
queued events or interrupt a transaction; SQLite recovery verifies the next
startup. A thread cannot forcibly cancel arbitrary OS/disk I/O. No claim that
all enqueued data survived is made.

## Read-only inspection

- `/api/gate/status` adds `forward_research` status/counts, queue depth and the
  last committed source observation timestamp. `checked_at` is the cache
  calculation time, not proof the market data is current.
- `GET /api/gate/research/forward` returns collection state plus the cached
  per-symbol/strategy baseline/confluence/cash report. A blocked collector
  returns HTTP 503 with no financial comparison; disabled returns its explicit
  state. No request creates files, replays data or authorizes orders.
- Per-account source candle/receipt times remain visible. Recovery can yield
  old data: `data_is_current_proven=false`. This is not an exchange mark price,
  fill, portfolio return or live execution result.
- The journal retains the primary 15m candle and public versioned hypotheses;
  it does not contain the complete 200-candle 1h/4h analysis context. Replay
  verifies the financial simulation of recorded hypotheses, not an independent
  reconstruction of every S1/S2 input or an attested exchange delivery clock.

## Validation and release gates

The dedicated local suite passed **212 tests, with 1 skipped**, including
36 new cases: all seven disabled flag
combinations; actual S1/S2 public snapshots using exactly the existing nine
series; capture isolation; 3-market service integration; open-position restart
then close after costs; repeated-candle first receipt; altered candles/gaps;
disk failure, disk reserve, slow disk without holding S1's event loop; queue
overflow; second writer; symlinks/unrelated DB/corrupt heads; commit/report
failure; capacity; partial shutdown and cached API behavior.

Tests use isolated temporary journals and synthetic candles; they do not prove
continuous hosted operation or financial edge. Dedicated and full CI must pass
on the published head. S1 audit outcome, S2E passive rollout, approved S3 path,
disk headroom, real-runtime shutdown/recovery, gaps, latency and available data
must be checked before activating this integration. Any source revision/flag
rollback must be reviewed together; disabling S3 preserves its journal and
does not delete any evidence. No automatic strategy promotion or TESTNET/LIVE
order release exists here.
