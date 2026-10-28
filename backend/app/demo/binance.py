"""Read-only Binance USD-M Futures Demo client.

Only the exact Demo origin and an explicit GET allowlist are reachable. This
module cannot place, cancel or change orders, leverage or account settings.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

DEMO_ORIGIN = "https://demo-fapi.binance.com"
KEY_VARIABLE = "CAPITAL_CIPHER_BINANCE_DEMO_KEY_ID"
SECRET_VARIABLE = "CAPITAL_CIPHER_BINANCE_DEMO_SIGNING_SECRET"
PUBLIC_PATHS = frozenset({"/fapi/v1/ping", "/fapi/v1/time", "/fapi/v1/exchangeInfo", "/fapi/v1/klines"})
SIGNED_PATHS = frozenset({"/fapi/v3/account", "/fapi/v3/positionRisk", "/fapi/v1/openOrders", "/fapi/v1/accountConfig", "/fapi/v1/openAlgoOrders"})


class DemoError(Exception):
    """Stable error code; never include URLs, credentials or provider bodies."""

    def __init__(self, code: str, *, status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class DemoCredentials:
    key_id: str
    signing_secret: str

    def __post_init__(self):
        if len(self.key_id.strip()) < 8 or len(self.signing_secret.strip()) < 16:
            raise DemoError("INVALID_DEMO_CREDENTIALS")
        if any(c.isspace() for c in self.key_id + self.signing_secret):
            raise DemoError("INVALID_DEMO_CREDENTIALS")

    def __repr__(self):
        return "DemoCredentials(key_id='***', signing_secret='***')"


class BinanceFuturesDemoClient:
    def __init__(self, credentials: DemoCredentials | None, *, transport=None, clock_ms=None):
        self._credentials = credentials
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._offset_ms = 0
        self._clock_synchronized = False
        self._client = httpx.AsyncClient(
            base_url=DEMO_ORIGIN,
            timeout=httpx.Timeout(8.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def _get(self, path: str, params: dict | None = None, *, signed=False):
        allowed = SIGNED_PATHS if signed else PUBLIC_PATHS
        if path not in allowed:
            raise DemoError("DEMO_ENDPOINT_BLOCKED")
        pairs = list((params or {}).items())
        headers = {}
        if signed:
            if self._credentials is None:
                raise DemoError("DEMO_CREDENTIALS_MISSING")
            if not self._clock_synchronized:
                raise DemoError("DEMO_CLOCK_NOT_SYNCHRONIZED")
            pairs.extend([("recvWindow", "5000"), ("timestamp", str(self._clock_ms() + self._offset_ms))])
            signature = hmac.new(self._credentials.signing_secret.encode(), urlencode(pairs).encode(), hashlib.sha256).hexdigest()
            pairs.append(("signature", signature))
            headers["X-MBX-APIKEY"] = self._credentials.key_id
        try:
            response = await self._client.get(DEMO_ORIGIN + path, params=pairs, headers=headers)
        except httpx.HTTPError:
            raise DemoError("DEMO_NETWORK_UNAVAILABLE") from None
        if response.is_redirect:
            raise DemoError("DEMO_REDIRECT_BLOCKED", status=response.status_code)
        if response.status_code in {418, 429}:
            raise DemoError("DEMO_RATE_LIMITED", status=response.status_code)
        if response.status_code in {401, 403}:
            raise DemoError("DEMO_AUTH_OR_ACCESS_REJECTED", status=response.status_code)
        if response.status_code >= 400:
            raise DemoError("DEMO_REQUEST_REJECTED", status=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise DemoError("DEMO_RESPONSE_INVALID") from None
        if isinstance(payload, dict) and isinstance(payload.get("code"), int) and payload["code"] < 0:
            raise DemoError("DEMO_PROVIDER_ERROR")
        return payload

    async def sync_clock(self):
        self._clock_synchronized = False
        before = self._clock_ms()
        payload = await self._get("/fapi/v1/time")
        after = self._clock_ms()
        try:
            server = int(payload["serverTime"])
        except (KeyError, TypeError, ValueError):
            raise DemoError("DEMO_CLOCK_INVALID") from None
        if server <= 0 or not 0 <= after - before <= 3000:
            raise DemoError("DEMO_CLOCK_UNTRUSTED")
        self._offset_ms = server - (before + after) // 2
        self._clock_synchronized = True

    async def validate_account(self):
        await self.sync_clock()
        account = await self._get("/fapi/v3/account", signed=True)
        positions = await self._get("/fapi/v3/positionRisk", signed=True)
        orders = await self._get("/fapi/v1/openOrders", signed=True)
        mode = await self._get("/fapi/v1/accountConfig", signed=True)
        algo_orders = await self._get("/fapi/v1/openAlgoOrders", signed=True)
        assets_mode = mode
        if not isinstance(account, dict) or not isinstance(positions, list) or not isinstance(orders, list) or not isinstance(algo_orders, list):
            raise DemoError("DEMO_ACCOUNT_RESPONSE_INVALID")
        if not isinstance(mode, dict) or type(mode.get("dualSidePosition")) is not bool:
            raise DemoError("DEMO_POSITION_MODE_INVALID")
        if not isinstance(assets_mode, dict) or type(assets_mode.get("multiAssetsMargin")) is not bool:
            raise DemoError("DEMO_ASSET_MODE_INVALID")
        try:
            assets = [r for r in account["assets"] if r["asset"] == "USDT"]
            if len(assets) != 1:
                raise ValueError()
            asset = assets[0]
            wallet = _finite(asset["walletBalance"])
            unrealized = _finite(asset["unrealizedProfit"])
            equity = _finite(asset["marginBalance"])
            available = _finite(asset["availableBalance"])
            open_positions = []
            for row in positions:
                quantity = _finite(row["positionAmt"])
                if quantity:
                    open_positions.append({
                        "symbol": row["symbol"], "side": "BUY" if quantity > 0 else "SELL",
                        "quantity": abs(quantity), "entry_price": _finite(row["entryPrice"]),
                        "unrealized_pnl": _finite(row["unRealizedProfit"]),
                        "position_side": row["positionSide"],
                    })
        except (KeyError, TypeError, ValueError):
            raise DemoError("DEMO_BALANCE_OR_POSITION_INVALID") from None
        issues = []
        if mode.get("canTrade") is not True:
            issues.append("DEMO_TRADING_PERMISSION_MISSING")
        if mode["dualSidePosition"]:
            issues.append("DEMO_HEDGE_MODE_ENABLED")
        if assets_mode["multiAssetsMargin"]:
            issues.append("DEMO_MULTI_ASSET_MODE_ENABLED")
        if equity <= 0 or available <= 0:
            issues.append("DEMO_USDT_FUNDS_REQUIRED")
        if open_positions or orders or algo_orders:
            issues.append("DEMO_ACCOUNT_NOT_FLAT")
        return {
            "authenticated": True, "market": "USD_M_FUTURES_DEMO",
            "execution_enabled": False, "live_execution_available": False,
            "account_trading_enabled": mode.get("canTrade") is True,
            "api_trade_permission_verified": False,
            "one_way_mode": not mode["dualSidePosition"],
            "single_asset_mode": not assets_mode["multiAssetsMargin"],
            "wallet_balance_usdt": wallet, "unrealized_pnl_usdt": unrealized,
            "equity_usdt": equity, "available_usdt": available,
            "positions": open_positions, "open_order_count": len(orders), "open_algo_order_count": len(algo_orders),
            "account_preflight_passed": not issues, "issues": issues,
        }

    async def candles(self, *, symbol: str, timeframe: str, limit: int = 200):
        if symbol not in {"BTCUSDT", "ETHUSDT", "SOLUSDT"}:
            raise DemoError("DEMO_SYMBOL_BLOCKED")
        if timeframe not in {"1m", "5m", "15m", "1h", "4h"} or not 1 <= limit <= 1000:
            raise DemoError("DEMO_CANDLE_REQUEST_INVALID")
        await self.sync_clock()
        payload = await self._get("/fapi/v1/klines", {"symbol": symbol, "interval": timeframe, "limit": limit})
        if not isinstance(payload, list):
            raise DemoError("DEMO_CANDLES_INVALID")
        now = self._clock_ms() + self._offset_ms
        try:
            return [
                {"symbol": symbol, "timeframe": timeframe, "open_time_ms": int(row[0]),
                 "close_time_ms": int(row[6]), "open": _finite(row[1]), "high": _finite(row[2]),
                 "low": _finite(row[3]), "close": _finite(row[4]), "volume": _finite(row[5])}
                for row in payload if int(row[6]) < now
            ]
        except (IndexError, TypeError, ValueError):
            raise DemoError("DEMO_CANDLES_INVALID") from None

    async def aclose(self):
        await self._client.aclose()


def _finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite value")
    return result
