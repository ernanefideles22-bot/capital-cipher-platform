# Month 4 — leakage-resistant walk-forward protocol

This is the second Month 4 increment. It adds a versioned protocol for
evaluating one pre-registered strategy candidate across ordered train,
validation, and test windows. Every result remains `RESEARCH_ONLY`.

It does not tune a model, select a winning parameter set, or authorize live
trading. Durable report persistence is added by the next increment documented
in [`month-4-durable-experiments.md`](month-4-durable-experiments.md).

## Request

`POST /api/v1/backtest/walk-forward` requires administrator authentication and
accepts the same `store | inline | csv` data sources as a regular backtest:

```json
{
  "candidate_version": "SCALP_15M_v1",
  "backtest": {
    "symbol": "BTCUSDT",
    "timeframe": "15m",
    "source": "store"
  },
  "protocol": {
    "schema_version": "1.0.0",
    "protocol_version": "walk-forward-v1",
    "selection_mode": "pre-registered",
    "train_candles": 250,
    "validation_candles": 100,
    "test_candles": 100,
    "embargo_candles": 1,
    "step_candles": 100,
    "anchored_train": false,
    "max_folds": 20
  }
}
```

If `step_candles` is omitted, it resolves to `test_candles`. A smaller step is
rejected because it would count the same test candle in more than one fold.

## Temporal boundaries

For a rolling fold:

```text
[ TRAIN ][ embargo ][ VALIDATION ][ embargo ][ TEST ]
          excluded                excluded
```

The next fold advances by `step_candles`. With `anchored_train=false`, the
training window moves and keeps a fixed size. With `anchored_train=true`, its
start remains fixed and its end expands.

The planner:

- sorts candles by exchange close time;
- requires exactly one exchange/symbol/timeframe series;
- rejects duplicate timestamps;
- excludes embargo candles from every segment;
- requires non-overlapping test windows;
- fails closed when the dataset cannot produce one complete fold.

## Pre-registration, not pretend training

The current strategy runtime has no fitting interface. Consequently, the train
window is content-addressed and recorded but is not replayed through the
trading pipeline and cannot change the candidate.

`candidate_version` must exactly match the enabled, versioned strategy for the
requested symbol and timeframe. Validation and test each run in a fresh PAPER
pipeline with a fresh balance, risk state, order book, audit service, and
candle store. No position or equity crosses a segment boundary.

This conservative isolation means indicators warm up inside each validation
or test segment. A future version may add an explicitly versioned,
read-only-history warm-up contract, but it must not allow orders or state from
the warm-up period to leak into evaluation.

## Reproducible identity

`experiment_id` is a SHA-256 identity over:

- the complete candle dataset hash;
- candidate version;
- the full walk-forward protocol and resolved step;
- realistic execution assumptions;
- simulation context hash.

The simulation context contains the backtest-engine version, initial balance,
risk limits, and complete versioned strategy configuration. Every train,
validation, and test segment also receives its own content-addressed dataset
identity. Reordering identical input candles does not change the experiment or
fold identities.

## Reporting safety

Validation and test metrics are never combined. Each receives a separate
aggregate containing fold count, trade count, profitable-fold ratio, mean and
median PnL percentage, worst drawdown, and mean expectancy.

Ordinary backtest history is not polluted by internal fold runs. Walk-forward
results have separate report endpoints:

```text
GET /api/v1/backtest/walk-forward/reports
GET /api/v1/backtest/walk-forward/reports/{experiment_id}
```

The experiment response always contains:

```text
promotion_status = RESEARCH_ONLY
```

Neither positive validation nor positive test metrics can change runtime mode,
risk limits, strategy enablement, or execution permissions.

## Completed controls

The versioned train-only fitting boundary, pre-registered acceptance rules,
minimum sample sizes, multiple-testing correction, candidate budgets,
historical spread/funding series, and margin/liquidation mechanics are
implemented in [`month-4-completion.md`](month-4-completion.md).

Walk-forward output remains research evidence only and is not proof of
profitability, tradeability, or readiness for real capital.
