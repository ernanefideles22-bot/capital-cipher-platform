# Capital Cipher — S2C: causal Stage 1 + Stage 2 confluence, RESEARCH ONLY

Status **DRAFT, offline, never authorized for orders or deployment**.

## Source isolation

- Base is S2B commit `9c03597ef401361df1f2e7d9b4bb5e9e32ae17b7`. Gate Railway staging continues unchanged at `2eb009b`. No modification of the 300 original agents, original Gate S1 SHADOW, OMS, central risk, Postgres, Redis, persistent volume, permissions or billing.
- Stage 1 professional market reader creates one timestamp-locked 15m / 1h / 4h Gate Futures chart context, from independently validated closed candles, confirmed pivots, EMA regime, patterns and market structure.
- Stage 2's existing EMA pullback, volume breakout or range reversal makes a research plan based on the **preceding closed 15m candle only**. The deterministic confluence **may REJECT**, never create an opposite direction, invent an entry or modify the stop/target.
- Trend plans LONG require 4h + 1h `UPTREND`; SHORT require both `DOWNTREND`. An opposing 15m regime rejects. Bearish candle rejection blocks proposed LONG, bullish candle rejection blocks SHORT.
- Range reversal requires no strong up/down higher-timeframe trend and a `RANGE_OR_TRANSITION` 15m regime. No forced trading quota. Named candlestick patterns, Fibonacci and support/resistance are **descriptive**, not independently validated predictive entries; the current confluence does not pretend to use them as a profitable price target.
- Stale/future/mixed-source chart context, wrong symbol/timeframe, disagreement on cutoff, unauthorized trading or incomplete read-only evidence fails closed (exception); neutral/no signal returns WAIT.
- Replay callbacks are validated against original strategy name, symbol, timeframe, exact cutoff and explicit `execution_authorized=False` before anything enters the simulated position engine.

## Chronology: no higher-timeframe leakage

- At decision time `t`, only closed higher-timeframe bars with `closed_at <= t` are supplied; unclosed future 1h/4h candles cannot be observed. Stage 1 pivot confirmation requires two subsequent *already closed* bars. Signal at bar `i` can only be simulated at `i+1` open, with the unchanged stop-first gap/cost assumptions from S2A.
- A historical Gate public REST response is a **batch snapshot**; historical receipt timestamps are unavailable. The separate historical adapter explicitly models hypothetical bar-close availability by providing temporary receipt clocks of `closed_at+1µs`. It neither persists nor modifies original receipts and must never be used in live order execution. Live Stage 1 retains strict real receipt clock checks.
- Two-arm comparison uses exactly the same historical 15m OOS window, fees/slippage/funding, position sizing, one-position cap, kill switches and fills. Both policies are fixed **before** inspection, without selecting the best on OOS. Rejection may free a slot for a later trade; don't assume gated trades are a subset of standalone filled trades.

## Evidence and release gates

1. Unit tests: matching clocks, rejecting future or stale context, rejecting executable plan, proving 4h/1h future candles do not alter past verdict, rejecting contrary trends, regression of S2B/S2A/S1/OMS.
2. One-shot research-only diagnostic compares three fixed strategies independently for BTC/ETH/SOL 15m entries, filtered by synchronized 1h/4h. Outputs baseline vs gated *net* PnL, trade count, drawdown, and reasons for refusals. Nine comparisons on public Gate Futures Testnet data, no authenticated/write endpoint, no trading, no staging deployment.
3. A difference in hypothetical OOS performance is not proof of predictive alpha. These series are short and overlap across policies/strategies; no trade-frequency or confidence claim is justified without independent regimes, far longer historical periods, unchanged thresholds, actual Gate fee/slippage/funding calibration and repeated or prospective holdouts.
4. **No activation in OMS or existing 300 agents**. For later controlled SHADOW staging, require review of both CI workflows, no future leakage, and explicit operator approval before touching the Gate service. LIVE/TESTNET orders separately forbidden.

## Observed initial result, Gate Testnet public historical replay (1 Nov 2026)

The one-shot CI comparison ran **nine unselected fixed strategy experiments** (BTC/ETH/SOL × three strategies, 15m signals with 1h/4h context), each on 800 15m candles with a chronologically later 320-bar segment. Results are **overlapping independent hypothetical accounts** with US$1000 initial capital each and stress costs; the PnLs must **not** be interpreted as a single portfolio.

- S2 standalone: 109 simulated trades, sum of independent hypothetical account PnL **-US$194.410594**.
- S1+S2 context filter: 27 simulated trades, sum **-US$50.798227**; 148 candidate evaluations rejected by confluence.
- With **zero trades**, the no-trade benchmark PnL is **US$0** for every account. The filter beat the always-trading baseline in this restricted window but **still underperformed abstention**.
- Among nine comparisons, the gate shut off all three Bollinger/RSI mean-reversion branches, often because higher timeframes were trending. SOL 15m Donchian worsened (-6.55 vs -7.88), and SOL EMA remained unchanged and losing (-22.98 each).
- BTC 15m Donchian alone was -15.744107; filtered was +2.610379, but only two filtered trades: not a credible positive sample.
- **No candidate is validated for profitability, no executable trading access is granted.**

**Important selection limitation:** The same recent Gate market history had already been examined during Stage 2A/2B development. The 320-bar later slice is chronologically separated **within this diagnostic** but is **not a pristine, never-observed independent holdout**. Thus these results cannot establish generalization or alpha, even if some PnLs become positive. Before performance claims, freeze policies and test on genuinely new data and sufficiently large forward windows. In particular, reducing entries alone can improve observed drawdown without improving per-trade expectancy or beating a no-trade baseline.
