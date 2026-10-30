# Gate durable risk snapshot scope

The 2026-10-30 Gate owner-canary activation connected successfully but created no
OMS order and consumed no canary slot. Read-only inspection of the deployed
PostgreSQL database and existing SQLite journal established:

- latest Gate venue reconciliation MATCHED with no local/venue orders;
- a Gate REDUCED/approved evaluation and OMS_TESTNET_ORDER_REQUESTED audit entry;
- zero Gate OMS orders, zero journal cycles and zero canary reservations;
- two global PAPER position exposures and zero Gate Testnet exposures;
- the durable approval hash matched the Gate snapshot, not the global snapshot.

`VenueAwareRiskManager` correctly approves only the Gate Testnet portfolio.
`Repository.create_oms_order` then compared that approval with a global snapshot
that included unrelated PAPER positions, rejecting it as stale. This occurs
before any exchange dispatch and is independent of the stop API.

The atomic Gate TESTNET consumer now queries the same Gate-only snapshot: own
pending OMS reservations, latest Gate reconciliation positions and managed
fallback fills. It retains the hash comparison, row locks, shared durable kill
control, approval expiry, single-use consumption and payload checks. It does not
clear risk, reset a canary budget, delete history or change default PAPER and
other venue consumers. No schema change or new infrastructure is needed.

Regression tests reproduce the previous rejection with two PAPER positions in
the same repository, then prove the simulated owner canary entry/stop/exit and
identical PAPER history. A separate test confirms the scoped snapshot retains
an owned pending Gate reservation; scope isolation is not a risk bypass.

Private research failure logs now retain only the exception class and a bounded
internal stage name, never exception text, credentials or account balances.
This makes admission failures diagnosable without logging secret-bearing SQL or
provider errors.

A new exact-revision grant is necessary after publishing this source correction.
The prior grant and immutable risk evaluations must remain as historical evidence.
No real Gate entry/stop/exit proof is claimed by these offline tests.
