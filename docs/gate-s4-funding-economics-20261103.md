# S4: BTC spot/perpetual funding economics, V1

After #95 rejected the SOL directional lead, this new hypothesis tests a different
source of return: long BTC spot and short the same BTC quantity in the perpetual,
receiving or paying the historical settled funding rate. It is a fixed-duration
economics reference, **not a risk-managed trading strategy or approval for S5**.

## Register before data

One instrument, one hypothesis, six fixed 30-day calendar blocks from
2024-04-01 00:00 UTC through 2024-09-28 00:00 UTC. This begins at the first full
calendar-quarter boundary inside already-used 4h training history. These dates
overlap past project research: no global holdout or prospective claim. The
existing reserve is not evaluated. No parameter grid or date replacement.

```bash
cd backend
python -m scripts.gate_s4_funding_feasibility register --root /absolute/new-study --output /absolute/registration.json
python -m scripts.gate_s4_funding_feasibility run --root /absolute/new-study --output /absolute/review.json
```

Registration exclusively creates an append-only SQLite journal containing the
rules and SHA256 of the engine. Run is once-only. Missing/irregular data, HTTP
errors, interruption and calculation failure do not permit retry/reset. Raw
responses are preserved before normalization; funding and three aligned price
series require their full registered grids. The public GET allowlist contains
only BTC spot candles, BTC perpetual/mark candles and BTC funding history. No
keys, private endpoints, account access or order submission.

## Paired reference

1000 USDT fresh capital for each block/scenario: 400 spot notional, 400 cash
allocated to the short margin, 200 reserve (including fees). Fixed BTC quantity
400 divided by adversely slipped spot entry; the same base units on the short.
Gross initial exposure must not exceed capital. No borrowing, interest income,
collateral transfers, reinvestment or position resizing. Initial gross exposure
does not remain constant as prices change. Fractional quantity is hypothetical:
historical lot sizes, minimum notional and fee currency are not verified.

Entry at the first 4h opening strictly after the calendar boundary, plus an
additional 4h in the delayed scenario; exit at the next fixed 30-day boundary.
Both legs use their own trade prices with adverse slippage. Four fills incur
fees and slippage. Nominal per side: spot fee 20 bps, perpetual fee 6 bps,
slippage 6 bps. Stress: 20/12/12. These are explicit assumptions, not verified
historical or account-specific fee quotes. No maker-fill or discount assumption.

Funding: only settled events strictly between entry and exit. Positive rates
credit the short, negative debit it. Use q × mark open × settled rate as a
4h payment proxy. Also compute conditional lower/upper payment sensitivities
using extrema across the two candles adjacent to settlement; reverse their
roles for negative rates. These depend on the event mark falling within those
coarse historical bounds; they are not exact settlement ticks or guaranteed
receipt bounds. Actual funding cadence must match the fixed 8h source grid;
otherwise the V1 source fails rather than imputing or adapting its schedule.

Separate basis price PnL, slippage, four fees and funding. Show cash at zero
under the no-interest assumption and the same-path funding removal as an
arithmetic decomposition, not an independently simulated alternative strategy.
Point and lower-proxy equity are relative to the entire 1000 USDT capital,
including short collateral and unused reserve; do not call a return on short
notional a return on capital. Blocks/scenarios are not an aggregate portfolio.

## Margin, risk and the screen

Combined price exposure can offset while the short margin fails. Monitor a
conservative 4h short-margin proxy: allocated 400 plus worst mark-high short PnL,
adverse cumulative funding and fees/slippage for closure, minus an assumed 2%
maintenance requirement. Ignore positive intrabar funding until after the
worst-margin check. This proxy is not historical liquidation reconstruction;
margin tiers, fee rules, event ordering and account transfers remain unverified.

All six blocks in all three scenarios must show positive lower-proxy net,
at least 88 funding events, closing-value drawdown at most 5%, and minimum
margin-proxy buffer at least 20% of allocated short margin. This is a new
**economic triage**, not a relaxation of directional-strategy promotion rules.
Six calendar blocks do not satisfy the old 30-completed-trade evidence gate.
No bootstrap, p-values or annualized expected income claims. Even a pass only
justifies considering execution research; profitability, S5 eligibility and
execution authorization always remain false.

No stops or OMS/risk execution are simulated here. The existing daily/total
loss limits and risk engine remain unchanged; this reference is deliberately
unconnected to them. A later executable pair strategy would have to implement
atomicity, residual exposure, margin controls, stop/flatten behavior and the
existing risk limits, then obtain sufficient new confirmation evidence. A
reference rejected here cannot be rescued by editing thresholds after the run.

## Primary references

- [Gate public funding and futures/mark data](https://www.gate.com/docs/developers/apiv4/en/futures/)
- [Gate spot field mapping](https://www.gate.com/docs/developers/apiv4/en/spot/)
- [Gate funding cash-flow explanation](https://www.gate.com/learn/articles/gate-futures-fees-and-funding-rates)
- [BIS, Crypto carry](https://www.bis.org/publications/working-paper-1087-crypto-carry)

The BIS research motivates investigating carry and its margin risks; it does
not validate this specific perpetual strategy, venue, period or fee model.
Custody, venue failure, leg timing and book liquidity remain outside this
coarse screening exercise. No integration, deployment or production setting
changes are part of this PR.

## V1 retention failure and V2 official archive transport

V1 was registered at 2026-11-03T20:16:19.444806Z. Its first funding GET returned
HTTP 400 with `from time exceeds 180-day limit`. Its journal is permanently
SOURCE_UNAVAILABLE, with zero financial evaluations; do not reset it.

The [official historical-data description](https://www.gate.com/developer/historical_quotes)
also documents monthly `futures_usdt/funding_applies` files at
`https://download.gatedata.org/futures_usdt/funding_applies/YYYYMM/BTC_USDT-YYYYMM.csv.gz`,
with timestamp and executed funding rate, distinct from forecast updates.

V2 requires that specific preserved V1 no-PnL error and stores its journal and
receipt hashes. It separately preregisters six archive months (April–September
2024), nine public price pages, and one source attempt before downloading.
The full V1 paired economics/date/cost/risk manifest is embedded unchanged.
Monthly files can contain rows beyond the exact study boundary; those rows
are not scored. No monthly source choice depends on returns. Missing or
irregular funding remains a terminal source failure, not zero funding.

```bash
cd backend
python -m scripts.gate_s4_funding_archive_v2 register --root /absolute/archive-study --prior-root /absolute/failed-v1 --output /absolute/archive-registration.json
python -m scripts.gate_s4_funding_archive_v2 run --root /absolute/archive-study --output /absolute/archive-review.json
```

Preserve both attempts. Switching from the rate-limited endpoint to the official
archive is a documented transport adaptation, not another parameter search or
independent confirmation. Source availability must not be reported as financial
acceptance or rejection. All previous execution/profitability limits apply.
