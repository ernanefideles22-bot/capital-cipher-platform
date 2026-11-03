# Bounded public execution-cost audit

Before another economic hypothesis, replace unsupported execution-cost certainty
with an explicit public-book measurement and a separate personal-fee gap.

Register before source capture. Fixed BTC/ETH/SOL, spot and linear USDT perpetual;
five observations each, with at least five seconds between batches. Six metadata
GETs and 30 order-book GETs (100 levels); no retries, order API or account access.
Retain every response/error and exclude stale, crossed, invalid or unsynchronized
books without silently replacing them. An append-only journal freezes source,
sample counts, clocks and target notionals. No backtest reserve or strategy replay.

```bash
cd backend
python -m scripts.gate_execution_cost_audit register --root /absolute/new-audit --output /absolute/registered.json
python -m scripts.gate_execution_cost_audit run --root /absolute/new-audit --output /absolute/review.json
```

Measure displayed buy/sell VWAP at 50/100/400/1000 USDT **per leg**, for independent
spot/perpetual quotes and equal-base-quantity pairs. Futures sizes are contract
counts multiplied by current quanto_multiplier; choose whole contracts even when
fractional contracts may be supported. Spot uses current quantity precision.
Paired sizes use the LCM of both lot increments, not a rounded residual hedge.
Respect published size/amount constraints and displayed depth. A paired 50-USDT
leg quote does not imply a fully funded strategy fits in a 50-USDT account.

Book current/update clocks: spot milliseconds, futures seconds. At receipt,
generation age at most 10 seconds, update age at most 60, future tolerance 2,
request duration at most 20. Pair generation skew at most 5 seconds. These
observations are concurrent REST snapshots, not synchronized executable quotes.

Buy and sell costs relative to mid include spread once; walking adds depth impact.
A same-book round trip is counterfactual: no fills, queue priority, adverse
selection, latency-path slippage, fee-currency inventory effects or future exit
liquidity are measured. Five samples cannot estimate robust tail percentiles or
justify lowering prior 6/12-bps per-side slippage assumptions, nor overwrite
historical cost assumptions with today's books.

Nominal/stress fee scenarios retain prior 20/6 and 20/12 bps per side on
spot/perpetual. Public contract fees are informational, not verified personal
fees. The deprecated spot fee metadata is not used to price fees. Account-specific
production rates require authenticated GET /wallet/fee (or a verified account
statement) for the correct market/settlement. The existing demo account client
targets Testnet; do not repurpose its credentials for production. This audit
never reads credentials or calls that private endpoint. Personal all-in execution
cost therefore remains UNVERIFIED_NOT_QUERIED, even with valid public books.

No hosted route, OMS/risk change, execution enablement, promotion, merge or deploy.
This is execution research infrastructure, not an economic signal or proof of
profitability. Preserve prior rejected strategies and their journals unchanged.

Primary references: [spot metadata/books](https://www.gate.com/docs/developers/apiv4/en/spot/),
[futures contract units/books](https://www.gate.com/docs/developers/apiv4/en/futures/),
[personal fee endpoint](https://www.gate.com/docs/developers/apiv4/en/wallet/).
