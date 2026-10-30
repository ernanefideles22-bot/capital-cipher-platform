# Gate-only offline replay evidence

Produced on 2026-10-30 from the proposed `fix/gate-demo-owner-release` source,
using the actual command `python -B scripts/run_month11_shadow_validation.py`.
The Git commit containing this file identifies the source subsequently checked
by CI; the report does not claim to have run on the previously deployed revision.

- SHA-256 of `shadow-replay.json`:
  `ff202d2432502dbe3f247f0ebc2bc790ab4c55c4390b32ce2c5fd71c09ff0721`.
- Result: PASSED; 300 registered PAPER agents; 1,800 executions; zero failures.
- Eight reconciliation runs; zero critical drift; zero orders before and after.
- External collectors disabled; zero IP/DNS attempts under the CLI audit hook.
- Gate-labeled synthetic candles; not real Gate order, stop or exit evidence.
- Seven replay days are not seven elapsed observation days or proof of profit.

This is technical preparation. It is neither owner acceptance of a real canary
nor an independent audit attestation. Real canary evidence must be captured on
the existing account-bound Gate journal after the bounded testnet release.
