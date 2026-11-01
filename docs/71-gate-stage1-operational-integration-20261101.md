# Capital Cipher — Gate Stage 1 integration to deployed service branch

Base: `fix/gate-stop-expiration-86400-20261030`, matching actual Railway
`capital-cipher-gate-testnet` source. This PR is intentionally separate from
the earlier research PR #70; do not deploy its research branch directly
because it lacks the Gate operational service. Stage 1 is **not fully complete**
from code/tests alone.

## Existing operational behavior preserved

- Keeps the existing 300-agent `app.demo.analysis` unchanged, including
  original decision consolidation and existing OMS/risk and persistent journal.
- Keeps the Gate futures API adapter `app.demo.gate_market.fetch_candles`
  unchanged: only GET public endpoints and contract-volume multiplier.
- Adds a separate, opt-in `GateProfessionalChartShadowAgent` consuming the
  Gate public closed candle history from the same adapter, with `NEUTRAL`
  or `WAIT` only; never forwards its output to the existing `process_analysis`
  OMS path.
- Default `GATE_S1_SHADOW_ENABLED` **absent = DISABLED**; no additional
  network requests, scheduled jobs, resource allocations or changes to
  Testnet API keys. If later explicitly set to `true`, a 15-minute research
  monitor fetches 200 bars x 3 timeframes per 3 symbols; results live only
  in the existing service's in-memory `/api/gate/status` and sanitized
  `GATE_S1_SHADOW_STATUS` log messages. No new volumes/tables/paid worker.
- The multi-timeframe `as_of` is sampled **after** the actual GET response.
  Historical candles from a batch share the real observation receipt time;
  they are **not** retroactively presented as real-time observations at
  their original closes. Two-right-bar confirmed pivots prevent lookahead.
- In the original 300-agent subprocess, default `received_at` uses subprocess
  clock but agent input timestamp uses the last bar close: this is a
  preexisting provenance inconsistency. It must be addressed in its own
  change with before/after regression evidence, **not** by silently changing
  live decision timestamps in this isolated Stage 1 PR.

## Acceptance and remaining blockers

1. Pass `Gate Professional Trader Stage 1 Research Gate` CI: stage1 chart,
   actual SHADOW agent, service opt-in/opt-out, legacy Gate tests and OMS.
2. Pass monorepo CI.
3. Review deployed Gate service writes/flags, data mounts and environment
   staged changes before any staging deployment, keep rollback.
4. Deploy same image/source to the same Gate service only after explicit
   operator approval. First deploy with `GATE_S1_SHADOW_ENABLED` absent to
   prove noninterference in actual staging.
5. Enable market-only SHADOW option in separate reviewed deployment.
6. Confirm authentic TestNet public candles for BTCUSDT, ETHUSDT,
   SOLUSDT on 15m/1h/4h; validate quote/volume units, gaps,
   newest-bar freshness, clocks, representative patterns, 24h reliability
   and existing 300-agent P&L unchanged from isolated addition.
7. No execution enablement is part of this scope. Strategy profitability
   is still unproven, and passing tests does not meet an economic gate.

Stage 2 must not start until Stage 1 passes the operational gate.

Links: earlier chart research PR #70; existing Gate integration tested here
in a branch based on the actual deployed service commit.
