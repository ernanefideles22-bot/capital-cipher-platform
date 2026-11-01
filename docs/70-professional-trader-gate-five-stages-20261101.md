# Capital Cipher Professional Trader — five sequential release stages

**Owner objective:** develop an evidence-driven professional trading system,
with traditional chart/context skills and the ability to research new strategies,
ultimately selecting only strategies with measurable net advantages.

**New venue target:** Gate.io Futures **TestNet**. No new Binance integration.
Historical immutable Binance research remains intact; do not revise previously
registered backtest outcomes or relabel the E2.6 Binance-specific protocol as Gate.

**Nonnegotiable guardrails:** preserve PostgreSQL/Redis, all volumes, OMS, risk
engine and existing PAPER history. Do not provision paid services without
operator approval. LIVE and TESTNET *order writes* remain disabled until
explicit separate owner authorization. Research modules cannot change risk,
submit orders, adjust live strategies or promote themselves.

## Sequential acceptance gates

| Stage | Deliverables | Evidence to graduate |
|---|---|---|
| S1 Professional market reading | Gate futures canonical OHLCV; *causal* candle patterns, confirmed market structure, S/R, validated Fibonacci anchors, trend context, volatility/volume and 15m/1h/4h alignment; readable evidence | Unit/property tests; no-lookahead checks; Gate TestNet read-only end-to-end data; clock/gap/finality/volume-unit checks; empirical pattern labeling review across 3 contracts and 3 timeframes; latency and stale-data telemetry |
| S2 Traditional executable strategy playbooks | Versioned trend pullback, breakout/false breakout, volatility breakout, range mean reversion, momentum; entry, invalidation, stop, target, cooldown and explicit abstention | Same-clock cost-aware matched backtests; fee, spread, slippage, funding and liquidation stress; minimum samples; independent untouched out-of-sample folds; negative strategies not promoted |
| S3 Contextual decision engine | Multi-timeframe confluence, regime-conditioned decisions, separate signal quality/confidence calibration, one-way position ownership and risk limits | Demonstrable *incremental* net benefit over S2 controls; no overlapping positions or silent hedge; counterfactual ablations and adverse execution tests |
| S4 Governed AI strategy research | Agents formulate, version and register falsifiable strategies/feature combinations; independent experiment queue, immutable dataset hashes and budgeted searches | Pre-registered train/test partitions, purged walk-forward and untouched final holdout, correction for multiple hypotheses, reproducible independent verification; no self-promotion |
| S5 Professional candidate selection and operation | Scorecards, SHADOW tracking, risk-adjusted allocation, change control, drift retirement, human review and supervised Gate TestNet OMS promotion | Prospective Gate-compatible event/filled-order reconciliation; positive net expectancy through out-of-sample conditions and conservative costs; controlled drawdown; operational alerts, rollback and kill switch; separate owner approval for any order writes |

A stage cannot be labelled *100% complete* from a green CI alone.
A code path that is built but not connected to the intended data/consumer
is **implemented, NOT operationally validated**. Pattern detection is not
economic edge; TestNet fills/prices alone are not evidence of live liquidity.

## S1 implementation slice — chart intelligence, Gate futures only

Files:
- `backend/app/strategy/professional_gate_chart_stage1.py`
- `backend/app/strategy/gate_futures_testnet_feed_stage1.py`
- `backend/app/agents/gate_professional_chart_shadow.py` (opt-in SHADOW agent, not registered automatically)
- `backend/scripts/run_gate_chart_stage1_readonly.py`
- `backend/app/tests/test_professional_gate_chart_stage1.py`
- `backend/app/tests/test_gate_futures_testnet_feed_stage1.py`

**Causality:** every candle must be closed, received and available by `as_of`;
contiguous exact timeframe steps and uniform `GATE` symbol/timeframe are
required. Old or incomplete data block, never fill with invented candles.
History batches may share the same receipt timestamp but are not prospective
as-of evidence. Two right-side closed bars confirm a pivot; do not surface a
swing before that confirmation. 15m / 1h / 4h must be independently available.

**Chart semantics:** doji, hammer/shooting-star morphology, engulfing,
inside/outside and long body are objective pattern labels, not trade entries.
Double tops/bottoms require confirmed pivots and neckline; unbroken patterns
are explicitly labelled `POTENTIAL`. Trendline slopes, nearest confirmed
support/resistance and anchored Fibonacci 38.2/50/61.8 are *descriptors*.
All report types set `trading_authorized=False`; no order or OMS imports.

**Gate feed:** public `GET /futures/usdt/candlesticks` exclusively on the
currently documented Gate API v4 TestNet
`https://api-testnet.gateapi.io/api/v4`. Contract identifiers are
`BTC_USDT`, `ETH_USDT`, `SOL_USDT`. Candle `t` is treated as
opening epoch seconds; a returned current unclosed interval is excluded.
The returned `v` is converted from contracts into base-asset quantity
using the public Gate `quanto_multiplier` obtained from
`GET /futures/usdt/contracts/{contract}`. This is **not USDT notional**.
API contract `BTC_USDT` is normalized to internal symbol `BTCUSDT`,
matching the existing Gate TestNet service's `gate_market.py` convention.
The same mapping applies to ETH and SOL. These semantics require real endpoint confirmation
during the S1 operational verification gate, not just mocked CI.

CLI entrypoint (not executed by PR/CI, intentionally unscheduled):

```sh
cd backend
python -m scripts.run_gate_chart_stage1_readonly
```

This command makes **public market-data GETs only** and does NOT read
`GATE_DEMO_OWNER_GRANT_JSON`, use existing private API keys, start TestNet
order routing, write any database, or schedule persistent collection.

## S1 remaining *operational* gate before graduation

1. Isolated regression and monorepo CI green, with Gate testnet HTTP mocks.
2. On a reviewed, authorized execution environment, run the read-only probe
   and verify 9 Gate contract/timeframe combinations return complete,
   fresh, correctly normalized closed candles and real timestamps.
3. Compare hand-labelled, independently inspected examples of each detector
   and validate pivot confirmation at prefix boundaries.
4. Confirm performance/latency and data gaps under disconnections, rate
   limits and delayed receipt. No claim of reliability without measurement.
5. Version and connect the validated reader to SHADOW research agents via
   explicit review; demonstrate logs/evidence from **running agents**, not
   just a standalone script.
6. Only then mark S1 *operationally complete* and begin S2. Never claim
   profitability merely because a pattern reader has been built.

Gate API v4: https://www.gate.com/docs/developers/apiv4/en/

## Stage 1 SHADOW agent contract (implemented, not deployed)

`GateProfessionalChartShadowAgent` is an optional, versioned
`CandleSpecialistAgent` using the existing `CandleStore` and `AgentInput` /
`AgentOutput` contracts. It reads GATE-only 15m, 1h and 4h bars with a
strict timestamp cutoff, labels technical evidence and returns **NEUTRAL**
with confidence **0**. If any required market data is absent/stale it returns
`WAIT`, never `BUY` or `SELL`. Registry tests prove `SHADOW` role and
non-inclusion in `decision_agents()`.

This class is not auto-registered into the existing 300-agent runtime.
Therefore it cannot be described as active in staging until an explicitly
reviewed backend rollout and real Gate-input verification. Its existence
does not alter candidate promotion, execute a trade or authorize testnet
orders. The operational S1 gate still requires observing the actual
registration and the emitted evidence in staging SHADOW runs.

**Existing Gate service compatibility:** before wiring S1 into staging,
verify the deployment branch `fix/gate-stop-expiration-86400-20261030`
(and its `app.demo.gate_market` adapter) against the S1 normalizer on
identical real testnet responses. The two branches are not yet merged;
no second production feed or conflicting runtime symbol namespace is enabled.
