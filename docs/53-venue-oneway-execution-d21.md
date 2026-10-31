# D2.1 — historical venue execution and shared one-way research ledger

**Scope:** offline, read-only research adapter; stacked on D2 PR #62 (plus D1 #61, D0 #60).
**No live trading, no Gate Testnet writes, no PAPER engine modifications, no Railway deploy.**

## What is implemented

`app.strategy.venue_research_execution`:
- `ContractSpec` accepts an explicitly identified Gate USDT futures contract
  plus contract multiplier, price tick and max contracts.
- `HistoricalQuoteTape` accepts **already-collected** timestamped bid/ask,
  top-of-book depth and taker fee data. A quote must have an observed time and
  a separately recorded receipt/availability time; no use before receipt.
  Every simulated execution rejects stale or missing quotes.
- The fill model uses ask for buys, bid for sells, adverse 12 bps/side
  **additional stress** by default, configurable impact proportional to the
  order size versus supplied top-level depth, and unfavorable tick rounding.
  Executed price includes spread/slippage; the ledger subtracts explicit
  taker fees only, so spread is not deducted twice.
- `OneWayPortfolio` aggregates SCALP/DAY/SWING signed integer contracts.
  Opposite orders reduce current inventory first; an oversized opposite order
  reverses the net position. Same-side additions update the weighted-average
  price. Fee, realized profit and positive/negative funding transfer are
  accounted for. `reduce_only` can only decrease absolute exposure without
  crossing zero. The offline book is restricted to **1x equity**.
- `FundingEvent` is applied at settlement; a rate observed only after a
  settlement is NOT silently backfilled as if the trader knew it then.
- `replay_venue_portfolio` executes order intents in a chronological,
  pre-specified sequence, refusing ambiguous same-time ordering or repeat
  source identities. Reports are research-only and cannot authorize trades.
- Offline regression tests exercise cross-strategy reduction/reversal, funding,
  costs, stale and future data, reduce-only enforcement, missing margin and
  duplicate identities.

## Grounding in Gate documentation

- One-way mode is `single` and hedge is `dual`; this must be confirmed
  against the actual trading account before any separate execution testing.
- A futures contract has a `quanto_multiplier`, price tick and fee metadata.
  Gate's `reduce_only` flag prevents a reverse-opening order.
- Official funding rate history is available via GET
  `/futures/{settle}/funding_rate`; futures order book and trades endpoints
  provide additional historical market context. A point-in-time collector
  and storage/archive are NOT implemented here.

Sources:
- https://www.gate.com/docs/developers/apiv4/en/futures/
- https://github.com/gate/gateapi-python/blob/master/docs/FuturesInitialOrder.md

## Known non-equivalences / hard stop before trading

1. **Top-of-book depth is not a complete fill simulation.** The linear
   market-impact stress is a conservative heuristic, not exact Gate matching,
   queue priority, partial fills, liquidations or maintenance-margin behavior.
2. Funding `mark_price`, rate and original receipt timestamps are supplied
   offline; their provenance must be independently verified. This module
   does not fetch, cache or attest live/historical Gate data.
3. The simulator currently accepts an explicit sequence of candidate order
   intents. D2 `CandidateReplayReport` from individual strategies is **not
   yet compiled to shared order intents**. Until that is implemented, we
   cannot claim cross-timeframe profit figures reflect portfolio netting.
4. One-way PnL here is realized gross minus fee/funding; spread and slippage
   are already embedded in fill prices. Do not subtract
   `HistoricalFill.spread_cost` or `adverse_slippage_cost` again.
5. Funding is modeled at timestamped settlements. Do not infer whether
   a historical rate was publicly knowable from its settlement timestamp alone;
   point-in-time source availability must be audited.
6. Exits, stop triggers, mark-price mode, liquidation and exchange order
   cancellations are not fully represented. Historical PnL is NOT an
   executable stop-loss acceptance test.
7. No qualified independent OOS profitability evidence, risk promotion,
   adaptive 300-agent optimization or trading permissions are added.

## Required D2.2

- Obtain permissionless historical quote/trade/funding data where possible,
  store trustworthy source IDs, observation and actual availability times.
- Link D2 strategy proposals to a **shared portfolio scheduler** before
  declaring strategy or portfolio profit.
- Replay conflicting cross-timeframe entry/exit/bracket orders with a single
  netted position and one coherent reduce-only stop policy.
- Benchmark fills against venue receipts and account metadata from an
  explicitly authorized, isolated Testnet campaign.
- Preserve signed backtest manifests and a sealed OOS holdout.
- Only after integrity and fidelity audits, submit SHADOW proposals for
  separate human approval; do not relax OMS or kill switch.
