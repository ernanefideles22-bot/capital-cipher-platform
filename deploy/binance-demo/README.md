# Binance USD-M Futures Demo — connection stage

This service validates the user's account created at https://demo.binance.com.
It is a standalone, **read-only connector**, not an operational Futures OMS.
It does not import `app.main`, run agents, open orders, or connect to PAPER's
PostgreSQL/Redis. Existing PAPER, Spot Testnet and Bybit settings are unchanged.

## Railway

Create an empty service named `capital-cipher-binance-demo` in the existing
project, with source pinned to this branch/commit and the Dockerfile
`deploy/binance-demo/Dockerfile`. Use `/health` for the Railway healthcheck.
No database, volume or Redis is needed for connection validation.

Set the following non-exchange configuration first:

- `DEMO_RESOURCE_NAMESPACE=capital-cipher-binance-futures-demo`
- `DEMO_ACKNOWLEDGEMENT=BINANCE_FUTURES_DEMO_ONLY_NO_REAL_FUNDS`
- `DEMO_EXECUTION_ENABLED=false`
- `DEMO_ADMIN_API_KEY`: independently generated random secret, >=32 characters
- `APP_SOURCE_REVISION`: pinned source commit

The service can start without exchange keys and reports `WAITING_FOR_CREDENTIALS`.
The user must enter both of these **Demo-only** values in Railway Variables:

- `CAPITAL_CIPHER_BINANCE_DEMO_KEY_ID`: the API Key from the Demo account
- `CAPITAL_CIPHER_BINANCE_DEMO_SIGNING_SECRET`: its Secret Key

Never put credentials in Git, screenshots, chat or the public page. Do not set
empty or invented credentials as placeholders. A partial pair fails startup.

`/api/demo/status` is public and contains no balances, positions or credentials.
`GET /api/demo/account`, `GET /api/demo/candles` and
`POST /api/demo/validate` require an `X-API-Key` header containing
`DEMO_ADMIN_API_KEY`. This key is not the Binance API Key.
`/ready` stays 503 until authenticated account preflight passes; its 200 means
read-only connectivity readiness, **never authorization to place orders**.
The account details are memory-only and revalidated every minute. Restarting
cannot restore a stale successful status. POST validate only refreshes local
reads; all requests sent to Binance are GET.

## API and boundaries

Only `https://demo-fapi.binance.com` is reachable; there is no configurable host,
no redirect following, and no proxy/environment trust. An explicit GET path
allowlist covers clock, account/config, positions, regular and conditional open
orders, and closed candles. HMAC-SHA256 signs each private GET after clock sync.
Account V3 contains balances; `/fapi/v1/accountConfig` contains `canTrade` and
position/asset modes. An authenticated read does not prove the API Key has TRADE
permission. The report explicitly returns `api_trade_permission_verified=false`.

The validation flags hedge mode, multi-asset mode, existing positions/orders and
missing USDT funds, without mutating the account. Credentials stay process-only;
provider bodies and signed URLs are excluded from errors/logs. Only configured
BTCUSDT/ETHUSDT/SOLUSDT candle reads are accepted; open candles are filtered out.

Official references (checked 2026-10-28):
- https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
- https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Account-Information-V3
- https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Account-Config
- https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Position-Information-V3

## Still required before agents can trade Demo

Implement an exact Futures execution adapter (not the Spot adapter), durable
risk approvals, venue-equity sizing and reconciliation specific to Binance
Futures; protective orders must use the current `/fapi/v1/algoOrder` API.
Validate entry, SL/TP, reduce-only exits, crash recovery and no duplicate writes
on the user's authenticated Demo account. Keep all automatic orders blocked
until those validations succeed. A successful connection or a Demo profit does
not establish strategy profitability with real capital.
