"""Authenticated Bybit V5 TESTNET adapter with asynchronous order semantics."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from app.core.errors import (
    AmbiguousExecutionError,
    ExecutionRejectedError,
    ExternalServiceError,
    SecurityError,
)
from app.execution.adapters.base import ExchangeExecutionAdapter
from app.execution.credentials import TestnetCredentials
from app.execution.precision import QuantityRules
from app.schemas.common import Exchange, OrderSide, utcnow
from app.schemas.oms import (
    ExecutionEnvironment,
    ExecutionFill,
    OMSOrder,
    OMSOrderStatus,
    OMSOrderType,
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueStateSnapshot,
)

BYBIT_TESTNET_BASE_URL = "https://api-testnet.bybit.com"


class BybitTestnetExecutionAdapter(ExchangeExecutionAdapter):
    exchange = Exchange.BYBIT

    def __init__(
        self,
        credentials: TestnetCredentials,
        *,
        base_url: str = BYBIT_TESTNET_BASE_URL,
        category: str = "linear",
        timeout_seconds: float = 5.0,
        receive_window_ms: int = 5_000,
        client: httpx.AsyncClient | None = None,
        clock_ms=None,
    ) -> None:
        normalized = base_url.rstrip("/")
        if normalized != BYBIT_TESTNET_BASE_URL:
            raise SecurityError("Bybit execution URL is not TESTNET")
        if category != "linear":
            raise ValueError("Month 7 supports only Bybit linear TESTNET")
        if timeout_seconds <= 0 or not 1_000 <= receive_window_ms <= 5_000:
            raise ValueError("Invalid Bybit TESTNET timing configuration")
        self._credentials = credentials
        self._category = category
        self._receive_window_ms = receive_window_ms
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=normalized,
            timeout=timeout_seconds,
            headers={"Accept": "application/json"},
        )

    async def healthcheck(self) -> bool:
        try:
            response = await self._client.get("/v5/market/time")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def prepare_order(self, order: OMSOrder) -> OMSOrder:
        self._validate_order(order)
        try:
            response = await self._client.get(
                "/v5/market/instruments-info",
                params={
                    "category": self._category,
                    "symbol": order.symbol.upper(),
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ExternalServiceError(
                "Bybit TESTNET instrument rules are unavailable",
                metadata={"error_type": type(exc).__name__},
            ) from exc
        if response.status_code >= 400:
            raise ExternalServiceError(
                "Bybit TESTNET instrument rules were rejected",
                metadata={"http_status": response.status_code},
            )
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ExternalServiceError(
                "Bybit TESTNET returned invalid instrument rules"
            ) from exc
        if payload.get("retCode") != 0:
            raise ExternalServiceError(
                "Bybit TESTNET instrument rules were rejected",
                metadata={"venue_code": payload.get("retCode")},
            )
        instruments = payload.get("result", {}).get("list", [])
        if (
            len(instruments) != 1
            or instruments[0].get("symbol") != order.symbol.upper()
        ):
            raise ExternalServiceError(
                "Bybit TESTNET returned an unexpected instrument"
            )
        lot = instruments[0].get("lotSizeFilter", {})
        return QuantityRules.from_strings(
            step=str(lot.get("qtyStep", "")),
            minimum=str(lot.get("minOrderQty", "")),
            maximum=str(
                lot.get("maxMktOrderQty")
                or lot.get("maxOrderQty")
                or ""
            ),
            minimum_notional=(
                str(lot.get("minNotionalValue"))
                if lot.get("minNotionalValue") is not None
                else None
            ),
        ).normalize(order)

    async def submit_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        self._validate_order(order)
        body: dict[str, object] = {
            "category": self._category,
            "symbol": order.symbol.upper(),
            "side": "Buy" if order.side == OrderSide.BUY else "Sell",
            "orderType": "Market" if order.order_type == OMSOrderType.MARKET else "Limit",
            "qty": _number(order.quantity),
            "timeInForce": _bybit_time_in_force(order.time_in_force.value),
            "orderLinkId": order.client_order_id,
        }
        body["positionIdx"] = 0
        if order.order_type == OMSOrderType.LIMIT:
            body["price"] = _number(order.limit_price)
        payload = await self._request(
            "POST",
            "/v5/order/create",
            body=body,
            write=True,
        )
        result = payload["result"]
        return VenueOrderSnapshot(
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            venue_order_id=result["orderId"],
            client_order_id=result.get("orderLinkId") or order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            status=OMSOrderStatus.SUBMITTED,
            quantity=order.quantity,
        )

    async def cancel_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        self._validate_order(order)
        body: dict[str, object] = {
            "category": self._category,
            "symbol": order.symbol.upper(),
            "orderLinkId": order.client_order_id,
        }
        if order.venue_order_id:
            body["orderId"] = order.venue_order_id
        payload = await self._request(
            "POST",
            "/v5/order/cancel",
            body=body,
            write=True,
        )
        result = payload["result"]
        return VenueOrderSnapshot(
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            venue_order_id=result.get("orderId") or order.venue_order_id or "",
            client_order_id=result.get("orderLinkId") or order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            status=OMSOrderStatus.CANCEL_PENDING,
            quantity=order.quantity,
            cumulative_filled_quantity=order.cumulative_filled_quantity,
            average_fill_price=order.average_fill_price,
        )

    async def fetch_state(
        self,
        *,
        symbols: set[str] | None = None,
    ) -> VenueStateSnapshot:
        observed_at = utcnow()
        order_rows = [
            *await self._paged(
                "/v5/order/realtime",
                {"category": self._category, "openOnly": 0, "limit": 50},
            ),
            *await self._paged(
                "/v5/order/realtime",
                {"category": self._category, "openOnly": 1, "limit": 50},
            ),
            *await self._paged(
                "/v5/order/history",
                {"category": self._category, "limit": 50},
            ),
        ]
        orders_by_id = {
            row["orderId"]: self._order_snapshot(row)
            for row in order_rows
        }
        orders = list(orders_by_id.values())
        fills = [
            self._fill_snapshot(row)
            for row in await self._paged(
                "/v5/execution/list",
                {"category": self._category, "limit": 100},
            )
        ]
        positions: list[VenuePositionSnapshot] = []
        if self._category == "linear":
            for row in await self._paged(
                "/v5/position/list",
                {"category": self._category, "settleCoin": "USDT", "limit": 200},
            ):
                quantity = abs(float(row.get("size", 0)))
                if quantity == 0:
                    continue
                if int(row.get("positionIdx", 0)) != 0:
                    raise ExternalServiceError(
                        "Bybit TESTNET hedge mode is outside the "
                        "Month 7 one-way safety boundary"
                    )
                positions.append(
                    VenuePositionSnapshot(
                        exchange=self.exchange,
                        environment=ExecutionEnvironment.TESTNET,
                        symbol=row["symbol"],
                        side=(
                            OrderSide.BUY
                            if row.get("side") == "Buy"
                            else OrderSide.SELL
                        ),
                        quantity=quantity,
                        entry_price=_optional_float(row.get("avgPrice")),
                        mark_price=_optional_float(row.get("markPrice")),
                        unrealized_pnl=float(row.get("unrealisedPnl") or 0),
                        observed_at=observed_at,
                    )
                )
        wallet = await self._request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED"},
            write=False,
        )
        balances: list[VenueBalanceSnapshot] = []
        for account in wallet.get("result", {}).get("list", []):
            for coin in account.get("coin", []):
                equity = float(coin.get("equity") or 0)
                available = float(
                    coin.get("availableToWithdraw")
                    or coin.get("walletBalance")
                    or 0
                )
                locked = max(0.0, equity - available)
                if equity == 0 and available == 0:
                    continue
                balances.append(
                    VenueBalanceSnapshot(
                        exchange=self.exchange,
                        environment=ExecutionEnvironment.TESTNET,
                        asset=coin["coin"],
                        available=max(0.0, available),
                        locked=locked,
                        equity=max(0.0, equity),
                        observed_at=observed_at,
                    )
                )
        return VenueStateSnapshot(
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            orders=orders,
            fills=fills,
            positions=positions,
            balances=balances,
            observed_at=observed_at,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _validate_order(self, order: OMSOrder) -> None:
        if (
            order.exchange != self.exchange
            or order.environment != ExecutionEnvironment.TESTNET
        ):
            raise SecurityError("Order is not for Bybit TESTNET")

    async def _paged(self, path: str, params: dict) -> list[dict]:
        rows: list[dict] = []
        cursor = ""
        for _ in range(20):
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            payload = await self._request(
                "GET",
                path,
                params=query,
                write=False,
            )
            result = payload.get("result", {})
            rows.extend(result.get("list", []))
            next_cursor = result.get("nextPageCursor") or ""
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return rows

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        write: bool,
    ) -> dict:
        timestamp = str(self._clock_ms())
        request_params = sorted(
            (key, str(value))
            for key, value in (params or {}).items()
            if value is not None
        )
        query = urlencode(request_params)
        body_text = (
            json.dumps(body, separators=(",", ":"), sort_keys=True)
            if body is not None
            else ""
        )
        signed_payload = (
            timestamp
            + self._credentials.key_id
            + str(self._receive_window_ms)
            + (body_text if method == "POST" else query)
        )
        signature = hmac.new(
            self._credentials.signing_secret.encode(),
            signed_payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY": self._credentials.key_id,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": str(self._receive_window_ms),
            "Content-Type": "application/json",
        }
        try:
            response = await self._client.request(
                method,
                path,
                params=request_params,
                content=body_text.encode() if body is not None else None,
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if write:
                raise AmbiguousExecutionError(
                    "Bybit TESTNET write status is unknown",
                    metadata={"error_type": type(exc).__name__},
                ) from exc
            raise ExternalServiceError(
                "Bybit TESTNET read failed",
                metadata={"error_type": type(exc).__name__},
            ) from exc
        if response.status_code >= 500 and write:
            raise AmbiguousExecutionError(
                "Bybit TESTNET write status is unknown",
                metadata={"http_status": response.status_code},
            )
        if response.status_code >= 400:
            error = (
                ExecutionRejectedError
                if write
                else ExternalServiceError
            )
            raise error(
                "Bybit TESTNET request rejected",
                metadata={"http_status": response.status_code},
            )
        payload = response.json()
        if payload.get("retCode") != 0:
            error = (
                ExecutionRejectedError
                if write
                else ExternalServiceError
            )
            raise error(
                "Bybit TESTNET business request rejected",
                metadata={"venue_code": payload.get("retCode")},
            )
        return payload

    def _order_snapshot(self, row: dict) -> VenueOrderSnapshot:
        quantity = float(row["qty"])
        filled = float(row.get("cumExecQty") or 0)
        average = _optional_float(row.get("avgPrice"))
        return VenueOrderSnapshot(
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            venue_order_id=row["orderId"],
            client_order_id=row.get("orderLinkId") or row["orderId"],
            symbol=row["symbol"],
            side=OrderSide.BUY if row["side"] == "Buy" else OrderSide.SELL,
            order_type=(
                OMSOrderType.MARKET
                if row["orderType"] == "Market"
                else OMSOrderType.LIMIT
            ),
            status=_bybit_status(row["orderStatus"]),
            quantity=quantity,
            cumulative_filled_quantity=filled,
            average_fill_price=average,
        )

    def _fill_snapshot(self, row: dict) -> ExecutionFill:
        occurred_at = datetime.fromtimestamp(
            int(row["execTime"]) / 1_000,
            tz=timezone.utc,
        )
        return ExecutionFill(
            fill_id=f"bybit:{row['execId']}",
            venue_order_id=row["orderId"],
            client_order_id=row.get("orderLinkId") or None,
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            symbol=row["symbol"],
            side=OrderSide.BUY if row["side"] == "Buy" else OrderSide.SELL,
            quantity=float(row["execQty"]),
            price=float(row["execPrice"]),
            fee=abs(float(row.get("execFee") or 0)),
            fee_asset=row.get("feeCurrency") or None,
            occurred_at=occurred_at,
        )


def _number(value) -> str:
    if value is None:
        raise ValueError("Numeric exchange parameter is required")
    return format(float(value), ".16g")


def _optional_float(value) -> float | None:
    if value in (None, ""):
        return None
    parsed = float(value)
    return parsed if parsed > 0 else None


def _bybit_time_in_force(value: str) -> str:
    return "PostOnly" if value == "POST_ONLY" else value


def _bybit_status(value: str) -> OMSOrderStatus:
    return {
        "Created": OMSOrderStatus.SUBMITTED,
        "New": OMSOrderStatus.SUBMITTED,
        "Untriggered": OMSOrderStatus.SUBMITTED,
        "Triggered": OMSOrderStatus.SUBMITTED,
        "PartiallyFilled": OMSOrderStatus.PARTIALLY_FILLED,
        "Filled": OMSOrderStatus.FILLED,
        "Cancelled": OMSOrderStatus.CANCELED,
        "PartiallyFilledCanceled": OMSOrderStatus.CANCELED,
        "Rejected": OMSOrderStatus.REJECTED,
        "Deactivated": OMSOrderStatus.EXPIRED,
    }.get(value, OMSOrderStatus.UNKNOWN)
