# Capital Cipher: economic diagnosis of frozen policies

Research only. The target is a demonstrated advantage after modeled costs,
not passing software tests. No policy is promoted by this change. The frozen
forward policy, journal, Railway audit and execution controls are unchanged.

## Recovered evidence, not new performance

`research/gate-s3g-20261102/original-s2c-summary.json` preserves the nine exact
records recovered from the successful Nov 1 S2C run at commit
`8813431f2d80408241e9befe2a25ab4c5c0245a5`.
Entry timeframe is 15m with 1h/4h context, 800 candles per timeframe,
last 320 bars evaluated without training selection. Prices came from Gate
**Testnet**, not production execution. Both arms use USD 1000 **independent
research accounts**, 0.25% risk/trade, max 1x notional, 6 bps fee and 6 bps
slippage per side plus modeled funding. Neither period is a pristine holdout.

| Market | Strategy | Baseline trades | Baseline net USD | Filtered trades | Filtered net USD |
| --- | --- | ---: | ---: | ---: | ---: |
| BTC | Donchian | 7 | -15.744107 | 2 | +2.610379 |
| BTC | EMA | 11 | -21.371625 | 4 | -3.635938 |
| BTC | Bollinger | 18 | -29.288989 | 0 | 0 |
| ETH | Donchian | 6 | -17.284346 | 1 | -2.775597 |
| ETH | EMA | 11 | -23.394345 | 5 | -16.135504 |
| ETH | Bollinger | 19 | -27.492900 | 0 | 0 |
| SOL | Donchian | 7 | -6.550459 | 5 | -7.881327 |
| SOL | EMA | 10 | -22.980240 | 10 | -22.980240 |
| SOL | Bollinger | 20 | -31.133366 | 0 | 0 |

Diagnostic sum across separate accounts: baseline 109 trades/-194.410594,
filtered 27/-50.798227. This is **not portfolio PnL**. Aggregate expectancy is
-1.783583 and -1.881416 USD/trade respectively; this change in average mixes
different market/strategy weights and does **not** identify a causal filter
effect. Cash has zero research PnL before financing/opportunity costs.

The filter prevented all Bollinger trading in this sample. It therefore showed
abstention, not a profitable Bollinger strategy. Higher-timeframe trend against
mean reversion appeared on all 108 rejected Bollinger proposals; reasons
overlap and cannot be added. SOL/EMA rejected no proposals. SOL/Donchian lost
more despite trading less. BTC/Donchian's two trades cannot establish an edge.

## Cost evidence from the separate S2B sample

`research/gate-s3g-20261102/original-s2b-summary.json` preserves nine records
at `9c03597ef401361df1f2e7d9b4bb5e9e32ae17b7`. This is a **different window**:
800 bars each at 15m, 1h and 4h, replay starting at index 80. Do not subtract
its costs from the S2C account results or combine its overlapping accounts into
a portfolio.

The 15m Bollinger baseline gross before explicit slippage drag was +16.007946
USD for BTC, +4.246799 for ETH and +6.486618 for SOL. Modeled total drag
(slippage + fees + funding) was respectively 105.520393, 81.104451 and
72.730180 USD; net was -89.512447, -76.857652 and -66.243561 USD.
This supports investigating trade economics/turnover before adding indicators.
Those raw gross values reconstruct saved fills and positions: they are **not**
a replay with zero friction (which could change brackets, sizing and paths).

There were 27 chronological walk-forward folds and **zero** with sufficient
OOS trades. Positive isolated full-history results in BTC/4h or SOL/1h/4h do
not establish validated candidates.

## New diagnostic protocol

The separate CLI `python -m scripts.gate_s3_economic_diagnostic`:

1. `--capture --output PATH` fetches only public Testnet GET candles using the
   existing normalizer. It records actual per-series batch receipt clocks.
   It saves a normalized candle snapshot and compressed-byte SHA-256 **before**
   evaluating policies. This is not a raw HTTP archive. Capture never overwrites
   an existing snapshot.
2. `--snapshot PATH --output OTHER_PATH` checks the adjacent checksum, then
   replays the same data without calling a network provider. It preserves
   snapshot receipts; only the existing historical adapter constructs explicitly
   hypothetical availability clocks. Checksums detect accidental changes, not
   prove vendor authenticity.
3. Runs unchanged baseline and S1-filtered policies under identical frozen
   costs/risk. It saves both full trade paths and every entry proposal evaluated
   on the filtered account path, including exact rejection reasons.
4. Classifies each **actually executed baseline trade** using the filter at
   that original decision time. It separates vetoed baseline winners/losers,
   allowed baseline entries and allowed entries absent from the filtered path.
   This does not pretend to simulate every overlapping possible opportunity.
5. Reconciles the account difference exactly as common-decision PnL difference
   + filtered-only trade PnL - baseline-only trade PnL. Equity-dependent sizing,
   occupancy and kill switches remain visible instead of attributing all change
   to vetoed trades. Per-reason baseline outcomes overlap and are nonadditive.
6. Records source hashes, costs, close-equity drawdown, boundary marks and PnL
   excluding boundary marks. It selects no candidate and mutates no policy.

The GitHub Actions workflow publishes an artifact containing snapshot,
checksum and full diagnostic, then verifies byte-identical replay. Artifact
retention is 90 days. The initial run's normalized snapshot and full traces
are also preserved in this PR under `research/gate-s3g-20261102/`, alongside
their checksums and provenance manifest, so they outlive artifact expiration.

## New archived capture: results

Source code `5227a0b9ca5e9d88d40918580b6066116d72bfe4`, successful workflow
[41395579441](https://github.com/ernanefideles22-bot/capital-cipher-platform/actions/runs/41395579441),
artifact 13118878914. Capture completed 2026-11-02 05:43:01 UTC. Snapshot SHA-256:
`02c406b670f8e802c2b48df1d1275f92b1a0683d8566d455e21ca4aa7be96eb5`.
First decision candle closes Oct 29 21:45 UTC; final mark Nov 2 05:30 UTC.
This window substantially **overlaps the previously observed data**. This is
an explanation of behavior, not an independent replication or fresh holdout.

| Market | Strategy | Baseline trades | Baseline net USD | Filtered trades | Filtered net USD |
| --- | --- | ---: | ---: | ---: | ---: |
| BTC | Donchian | 6 | -13.424401 | 2 | +2.610379 |
| BTC | EMA | 11 | -22.704740 | 3 | +0.470806 |
| BTC | Bollinger | 19 | -36.931853 | 0 | 0 |
| ETH | Donchian | 6 | -15.964701 | 1 | -2.775597 |
| ETH | EMA | 12 | -28.108808 | 4 | -12.595779 |
| ETH | Bollinger | 18 | -20.230522 | 0 | 0 |
| SOL | Donchian | 7 | -7.326580 | 6 | -4.345922 |
| SOL | EMA | 12 | -14.710015 | 12 | -14.710015 |
| SOL | Bollinger | 20 | -15.667071 | 0 | 0 |

Separate-account diagnostic sums: 111 trades/-175.068690 baseline,
28/-31.346128 filtered. They are not portfolio results. ETH/Donchian baseline
includes one boundary estimate, net -0.595326; SOL/Bollinger baseline includes
one boundary estimate, net +2.097562. Filtered accounts have no boundary marks.
Neither small positive BTC result supports activation or profit claims.

The full traces resolve several questions the old summaries could not:

- BTC/Donchian vetoed four baseline losses totaling -16.034780 USD. It retained
  one winner and one loser totaling +2.610379 USD. This observed pattern is
  encouraging but far too small and reused to establish predictive power.
- BTC/EMA vetoed seven losses and one winner. ETH/EMA vetoed six losses and
  two winners, but the four retained trades **all lost**. Vetoing trades can
  reduce total losses while worsening the remaining mean trade economics.
- SOL/EMA produced exactly the same 12 trades in both arms: the frozen filter
  contributed no selection there. Its reconstructed gross was +4.510354 USD,
  insufficient against approximately 19.220369 USD modeled costs.
- Bollinger vetoed all 57 baseline trades, including 24 net winners. It
  delivered inactivity. Reconstructed baseline gross/costs/net were:

| Bollinger 15m | Gross before explicit slippage USD | Modeled cost drag USD | Net USD |
| --- | ---: | ---: | ---: |
| BTC | +6.517733 | 43.449586 | -36.931853 |
| ETH | +18.113255 | 38.343778 | -20.230522 |
| SOL | +17.289667 | 32.956738 | -15.667071 |

All 28 filtered trade decisions also occur in the baseline in this snapshot.
There are no filtered-only decisions or allowed baseline trades missing from
the filtered path. The bridge nevertheless detects the small BTC/EMA sizing
difference (-0.018663 USD) after prior account losses; it does not silently
equate matched decisions with identical trade PnL. Costs are rounded to six
decimals in the table; the underlying trade bridge uses full precision.

To reproduce the archived evidence from the repository root:

```sh
cd backend
python -m scripts.gate_s3_economic_diagnostic \
  --snapshot ../docs/research/gate-s3g-20261102/snapshot.json.gz \
  --output /tmp/gate-s3g-archived-replay
gzip -dc ../docs/research/gate-s3g-20261102/economic-diagnostic.json.gz \
  > /tmp/gate-s3g-expected.json
cmp /tmp/gate-s3g-expected.json /tmp/gate-s3g-archived-replay/economic-diagnostic.json
```

The initial workflow's captured-data replay and a second local replay both
matched byte for byte. Source hashes refer to that archived run's diagnostic
implementation, not a later changed policy.

## Research decision

No current agent is established as profitable. No financial conclusion can be
inferred from passing engineering tests or surviving a 24h service audit.
The audit remains the prerequisite for deployment review, not proof of alpha.

The first hypothesis to investigate is that 15m mean reversion's expected price
movement is too small relative to turnover costs. A second is that rigid trend
alignment rejects opportunities without adding reliable predictive power.
Use new archived trade traces to distinguish those problems. Do not lower
friction assumptions, widen stops, optimize thresholds on this reused sample,
or activate the tiny positive cases to produce an attractive backtest.

Any future candidate V2 must be a separate version with an explicit hypothesis,
predeclared training/validation protocol, unchanged realistic costs and untouched
future evaluation data. Only then compare after-cost expectancy, loss tails,
drawdown and inactivity against frozen baseline and cash. No such V2 is
implemented or approved here; the prospective S3 policy remains frozen.

## Verification

Tests cover account-bridge algebra with mixed wins/losses and differing paths,
duplicate/corrupt accounting rejection, frozen-replay equality, veto outcome
reconciliation, future-context invariance, immutable receipts, mixed/gapped
provenance rejection, checksum tampering and source identity. The economic
workflow also requires a byte-identical replay of the captured dataset.
