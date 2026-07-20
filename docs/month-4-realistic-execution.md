# Month 4 — deterministic realistic execution model

This is the first Month 4 backtesting increment. It replaces one-sided,
fixed-price backtest costs with a versioned and reproducible execution model.
It does not add exchange connectivity, leverage, or live orders.

## Scope

Each backtest records the exact `realistic-v1` assumptions used:

```json
{
  "execution": {
    "schema_version": "1.0.0",
    "model_version": "realistic-v1",
    "taker_fee_bps": 8,
    "half_spread_bps": 1,
    "base_slippage_bps": 2,
    "volume_impact_bps": 10,
    "funding_rate_bps_per_8h": 0
  }
}
```

If the request omits `execution`, the server resolves these values from its
validated configuration. The resulting assumptions are embedded in the
report, so changing a later default cannot silently rewrite an old
experiment's meaning.

## Fill model

Market fills are adverse on entry and exit:

```text
participation = min(1, position_notional / candle_quote_volume)
impact_bps   = volume_impact_bps × sqrt(participation)
adverse_bps  = half_spread_bps + base_slippage_bps + impact_bps
```

A buy fills above the reference price and a sell fills below it. Closing a
long is a sell; closing a short is a buy. Zero-volume candles use full
participation and therefore the full configured impact rather than assuming
free liquidity.

Taker fees use executed notional. Spread, base slippage, volume impact, and
fees are reported separately. Spread and slippage are already represented in
the fill prices and are not subtracted twice from PnL.

## Funding

Funding is a signed configurable assumption because this increment does not
yet ingest historical funding-rate series:

```text
funding = position_notional
        × funding_rate_bps_per_8h / 10,000
        × elapsed_hours / 8
        × position_direction
```

With a positive rate, longs pay and shorts receive. Funding accrues from
candle timestamps, not wall-clock execution time, making repeated simulations
deterministic. A zero rate represents spot or an explicitly funding-neutral
scenario.

## Determinism and safety

- candles are still replayed sequentially with no future candle visible to an
  agent;
- order and equity timestamps come from replay data;
- the end-of-test position close uses the same adverse fill model;
- invalid negative or extreme assumptions fail Pydantic validation;
- PAPER operation keeps its existing execution path; `realistic-v1` is
  injected only into isolated backtests;
- a paper close is audited and persisted before in-memory balance or position
  state advances.

## Report additions

```text
execution_assumptions
fees
spread
slippage
volume_impact
funding
total_execution_cost
```

`total_execution_cost` is fees + spread + slippage + signed funding. Volume
impact is disclosed separately but is already included in slippage.

## Deferred Month 4 work

The next increments must add historical spread/funding datasets, margin and
liquidation mechanics, and out-of-sample acceptance criteria. The first
walk-forward protocol is documented in
[`month-4-walk-forward-protocol.md`](month-4-walk-forward-protocol.md).
Until then, this model is a conservative simulation assumption—not proof that
a strategy is tradeable or profitable.
