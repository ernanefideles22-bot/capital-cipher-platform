# Binance USD-M Futures Demo integration

The main Capital Cipher dashboard exposes a **Futuros Demo** area at `/#demo`.
The PAPER runtime remains PAPER; a server-only bridge sends authenticated Demo
operations to an isolated executor. Exchange credentials never enter the main
backend, frontend bundle, localStorage, agent subprocess or application journal.
No code path targets live exchange trading hosts.

## Service boundaries

Main service: `capital-cipher-backend`, existing PAPER database and risk engine.
Demo service: `capital-cipher-binance-demo`, exact Binance origin
`https://demo-fapi.binance.com`, separate persistent volume mounted at
`/var/lib/capital-cipher-demo`; `execution.sqlite3` uses WAL/FULL synchronization.
No PAPER PostgreSQL/Redis connections are accepted by the Demo service.

Main backend configuration (all server-only):
- `DEMO_CONNECTOR_URL=http://capital-cipher-binance-demo.railway.internal:8080`
- `DEMO_CONNECTOR_KEY=${{capital-cipher-binance-demo.DEMO_ADMIN_API_KEY}}`

Demo configuration:
- `DEMO_RESOURCE_NAMESPACE=capital-cipher-binance-futures-demo`
- `DEMO_ACKNOWLEDGEMENT=BINANCE_FUTURES_DEMO_ONLY_NO_REAL_FUNDS`
- `DEMO_EXECUTION_ENABLED=true` enables the protected execution capability;
  it **does not start automation**.
- `DEMO_WRITE_ACKNOWLEDGEMENT=VIRTUAL_FUNDS_ONLY_PROTECTED_FUTURES`
- `DEMO_STATE_ROOT=/var/lib/capital-cipher-demo`
- `DEMO_ADMIN_API_KEY`: independent random server-to-server secret
- `CAPITAL_CIPHER_BINANCE_DEMO_KEY_ID` / `CAPITAL_CIPHER_BINANCE_DEMO_SIGNING_SECRET`:
  user-provided Demo credentials; preserve them and never put them in Git/chat.
- `APP_SOURCE_REVISION`: pinned commit in both deployed services.

Use `/health` for Railway process health. `/ready` indicates authenticated
account-preflight readiness only. The first connection profile expects a funded,
flat, one-way, single-asset USDT account. Existing positions are never adopted
silently. Main dashboard private reads and all mutations require its existing
`ADMIN_API_KEY`. The operator key is kept only in React memory; it is distinct
from both the exchange API Key and the server-to-server connector key.

## Analysis and execution

The same 300-agent cohort is built in a bounded subprocess with an explicit
environment whitelist (PATH/PYTHONPATH/LANG only; no exchange/admin credentials).
Only closed, continuous, fresh Futures Demo candles enter analysis. Existing
MarketDataAgent, QuantAgent and TrendAgent retain primary decision authority via
DecisionEngine; other specialists stay shadow. Missing external evidence is an
explicit SKIPPED result. Central RiskManager evaluates candidates using actual
Demo account equity. The executor persists the proposal/risk approval and accepts
only a stored proposal identity; browsers cannot supply quantity, side, host or
SL/TP directly. Approvals expire after 60 seconds and may be used only once.

Execution is 1x isolated, one active position globally, at most 200 USDT notional,
0.1% equity risk including a conservative execution-cost allowance, minimum net
reward/risk 1.3, daily drawdown cap 2%, peak drawdown cap 5%, and three consecutive
confirmed losses. Venue precision/minimum notional and a 50-bps price-deviation
check may reject an otherwise approved candidate. Capital/risk are not borrowed
from the PAPER simulator.

Before each non-idempotent venue write, its deterministic identity and sending
state are durable. Requests are made once; ambiguous replies are reconciled by
identity rather than re-posted. Entries require immutable risk brackets; the
executor configures/verifies 1x isolated mode, submits a MARKET entry, then places
STOP_MARKET and TAKE_PROFIT_MARKET protections via `/fapi/v1/algoOrder`, using
`closePosition=true`, opposite side, BOTH position mode and MARK_PRICE triggers.
Both protections are read back. Protection failures cause a single reduce-only
emergency exit and halt. Missing protection on subsequent reconciliation also
forces a reduce-only exit. Recovered ambiguous entries are flattened and halted;
unknown writes never cause another entry submission. Flat positions have their
remaining conditional orders canceled and verified. Net PnL is populated only
when matched entry/exit fills and USDT commissions are available.

The durable journal records trade state, central-risk evidence, settings and a
hash-linked event history. A unique active-position index, unique risk approval,
and unique closed-candle signal prevent duplicates. Automation always starts
paused after restart/deploy. Kill-switch state survives restart. The main kill
switch also requests Demo halt/flatten and explicitly reports partial failure;
main execution/automation routes refuse entries while the central switch is on.
A technical halt reset requires a flat account and passing drawdown checks.

The initial automatic scope is ETHUSDT / 15m, one trade per closed candle.
Manual analysis may inspect BTCUSDT, ETHUSDT or SOLUSDT across the listed periods.
Automation requires a passed operational canary for the deployed source revision and an explicit authenticated
start action. The canary opens and protects one small ETHUSDT BUY, flattens it,
then does the same for SELL, and verifies no positions or regular/conditional
orders remain. It uses virtual funds, is recorded separately from agent trades,
and does not establish strategy profitability. SL/TP acceptance/readback and
reduce-only exits are verified; a canary does not claim every future trigger or
exchange outage has been tested. Full control/analysis/position/event views are
in the main frontend, not only the standalone connector status page.

## Official API references

Checked 2026-10-28:
- https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
- https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Account-Information-V3
- https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Account-Config
- https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order
- https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order
- https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Cancel-Algo-Order
