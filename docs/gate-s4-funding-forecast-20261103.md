# S4: past-only BTC funding forecast and cost gate

After #96 found that fixed 30-day carry generally failed its explicit four-fill
costs, test whether past funding can identify enough future rate budget. Freeze
one heuristic before downloading a new BTC period: no learned fit, grid, changed
fees, retrospective entry choice or reserved price history.

## Protocol

- BTC executed funding archives only, December 2022 through December 2023. Raw
  monthly files include unused rows past the exact study end; do not score them.
- 12 nonoverlapping 30-day outcome blocks beginning 2023-01-01, ending
  2023-12-27. December 2022 is warmup. This is a backward historical diagnostic
  selected after the 2024 losses, not prospective or certified globally unseen
  project history. Other research may share calendar dates or correlated assets.
- Decision at the block's initial 00:00 UTC. Only 90 past 8h settled rates from
  the prior 30 days enter the predictor. Model availability at settlement +1h;
  this is an assumption, not a verified historical publication receipt.
- Predicted rate budget = 88 × min(mean prior 7d, mean prior 30d). This heuristic
  minimum is not a statistical confidence bound. Entry clock +8h, exit at day 30;
  score exactly 88 settlements strictly between entry and exit.
- Eligibility: prediction at least 132 bps of normalized notional: 112 bps
  registered four-fill stress planning costs plus 20 bps basis uncertainty
  budget. Nominal 76 and stress 112 bps retain #96 fee/slippage assumptions.
- Compare MSE to an unfitted constant 1 bp per settlement baseline. A past-30d
  mean projection is also shown descriptively, never selected as a replacement.
- To justify a later pair replay, require at least six eligible blocks, every
  eligible future rate sum meeting 132 bps, and MSE strictly below the constant
  baseline. A zero baseline MSE yields no fabricated skill. No significance test.

Rates summed over constant hypothetical notional are **not cash funding on a
fixed BTC quantity, PnL, net trade returns or a guarantee of covering actual
costs**. Spot/perpetual/mark movements change notionals and basis; financing,
margin, liquidation, actual fees, fills and leg timing require later simulation.
The 132 bps screen can make false positive or false negative decisions. No price
replays are authorized in this diagnostic stage; six eligible rate windows would
still not satisfy the old 30-completed-trade promotion criterion.

## Source and integrity

Use Gate's documented monthly `futures_usdt/funding_applies` files from
`download.gatedata.org`, not forecast-update CSVs. Exactly 13 public GET attempts,
no account access or credentials. Persist compressed bytes and receipts before
parsing. Full 8h grid required; missing, duplicate, irregular or nonfinite data
fails closed. No source retry/reset or period replacement. Registration freezes
the engine hash and complete protocol in an exclusively created append-only
journal. Existing #96 fingerprints and evidence remain intact.

```bash
cd backend
python -m scripts.gate_s4_funding_forecast register --root /absolute/new-study --output /absolute/registered.json
python -m scripts.gate_s4_funding_forecast run --root /absolute/new-study --output /absolute/review.json
```

A successful forecast screen would only justify deciding whether to create a
new preregistered paired-execution study. Cash abstention is not a profitable
trade. Profitability, S5 eligibility, automatic promotion and execution remain
false regardless of diagnostics. No S3/OMS/risk integration, deployment or
production settings change.

Primary references: [Gate historical data](https://www.gate.com/developer/historical_quotes),
[Gate funding explanation](https://www.gate.com/learn/articles/gate-futures-fees-and-funding-rates),
[Gate public market API](https://www.gate.com/docs/developers/apiv4/en/futures/).
