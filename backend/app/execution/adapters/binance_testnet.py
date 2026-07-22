"""Authenticated Binance Spot Test Network adapter.

Only ``testnet.binance.vision`` is accepted. A timeout after a write is
ambiguous and is deliberately handed to reconciliation instead of retried.
"""

from __future__ import annotations

import hashlib
import hmac
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
    VenueStateSnapshot,
)

BINANCE_SPOT_TESTNET_BASE_URL = "https://testnet.binance.vision"


class BinanceTestnetExecutionAdapter(ExchangeExecutionAdapter):
    exchange = Exchange.BINANCE

    def __init__(
        self,
        credentials: TestnetCredentials,
        *,
        base_url: str = BINANCE_SPOT_TESTNET_BASE_URL,
        timeout_seconds: float = 5.0,
        receive_window_ms: int = 5_000,
        client: httpx.AsyncClient | None = None,
        clock_ms=None,
    ) -> None:
        normalized = base_url.rstrip("/")
        if normalized != BINANCE_SPOT_TESTNET_BASE_URL:
            raise SecurityError("Binance execution URL is not the Spot TESTNET")
        if timeout_seconds <= 0 or not 1_000 <= receive_window_ms <= 5_000:
            raise ValueError("Invalid Binance TESTNET timing configuration")
        self._credentials = credentials
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
            response = await self._client.get("/api/v3/ping")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def prepare_order(self, order: OMSOrder) -> OMSOrder:
        self._validate_order(order)
        try:
            response = await self._client.get(
                "/api/v3/exchangeInfo",
                params={"symbol": order.symbol.upper()},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ExternalServiceError(
                "Binance TESTNET instrument rules are unavailable",
                metadata={"error_type": type(exc).__name__},
            ) from exc
        if response.status_code >= 400:
            raise ExternalServiceError(
                "Binance TESTNET instrument rules were rejected",
                metadata={"http_status": response.status_code},
            )
        try:
            symbols = response.json().get("symbols", [])
        except (TypeError, ValueError) as exc:
            raise ExternalServiceError(
                "Binance TESTNET returned invalid instrument rules"
            ) from exc
        if len(symbols) != 1 or symbols[0].get("symbol") != order.symbol.upper():
            raise ExternalServiceError(
                "Binance TESTNET returned an unexpected instrument"
            )
        filters = {
            item.get("filterType"): item
            for item in symbols[0].get("filters", [])
        }
        lot = filters.get("MARKET_LOT_SIZE")
        try:
            market_step = (
                float(lot.get("stepSize", 0))
                if lot is not None
                else 0.0
            )
        except (TypeError, ValueError) as exc:
            raise ExternalServiceError(
                "Binance TESTNET returned invalid quantity rules"
            ) from exc
        if lot is None or market_step <= 0:
            lot = filters.get("LOT_SIZE")
        if lot is None:
            raise ExternalServiceError(
                "Binance TESTNET omitted quantity rules"
            )
        minimum_notional = None
        notional = filters.get("NOTIONAL")
        if notional is not None and notional.get(
            "applyMinToMarket",
            True,
        ):
            minimum_notional = notional.get("minNotional")
        legacy_notional = filters.get("MIN_NOTIONAL")
        if (
            minimum_notional is None
            and legacy_notional is not None
            and legacy_notional.get("applyToMarket", True)
        ):
            minimum_notional = legacy_notional.get("minNotional")
        return QuantityRules.from_strings(
            step=str(lot.get("stepSize", "")),
            minimum=str(lot.get("minQty", "")),
            maximum=str(lot.get("maxQty", "")),
            minimum_notional=(
                str(minimum_notional)
                if minimum_notional is not None
                else None
            ),
        ).normalize(order)

    async def submit_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        self._validate_order(order)
        params: dict[str, str | int] = {
            "symbol": order.symbol.upper(),
            "side": order.side.value,
            "type": order.order_type.value,
            "quantity": _number(order.quantity),
            "newClientOrderId": order.client_order_id,
            "newOrderRespType": "FULL",
        }
        if order.order_type == OMSOrderType.LIMIT:
            params.update(
                {
                    "price": _number(order.limit_price),
                    "timeInForce": _binance_time_in_force(
                        order.time_in_force.value
                    ),
                }
            )
        payload = await self._signed_request(
            "POST",
            "/api/v3/order",
            params,
            write=True,
        )
        return self._order_snapshot(payload, fallback=order)

    async def cancel_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        self._validate_order(order)
        params: dict[str, str] = {"symbol": order.symbol.upper()}
        if order.venue_order_id:
            params["orderId"] = order.venue_order_id
        else:
            params["origClientOrderId"] = order.client_order_id
        payload = await self._signed_request(
            "DELETE",
            "/api/v3/order",
            params,
            write=True,
        )
        return self._order_snapshot(payload, fallback=order)

    async def fetch_state(
        self,
        *,
        symbols: set[str] | None = None,
    ) -> VenueStateSnapshot:
        observed_at = utcnow()
        account = await self._signed_request(
            "GET",
            "/api/v3/account",
            {},
            write=False,
        )
        orders: dict[str, VenueOrderSnapshot] = {}
        fills: list[ExecutionFill] = []
        open_order_rows = await self._signed_request(
            "GET",
            "/api/v3/openOrders",
            {},
            write=False,
        )
        for row in open_order_rows:
            snapshot = self._order_snapshot(row)
            orders[snapshot.venue_order_id] = snapshot
        observed_symbols = {
            snapshot.symbol for snapshot in orders.values()
        }
        for symbol in sorted(set(symbols or set()) | observed_symbols):
            order_rows = await self._signed_request(
                "GET",
                "/api/v3/allOrders",
                {"symbol": symbol.upper(), "limit": 1_000},
                write=False,
            )
            for row in order_rows:
                snapshot = self._order_snapshot(row)
                orders[snapshot.venue_order_id] = snapshot
            fill_rows = await self._signed_request(
                "GET",
                "/api/v3/myTrades",
                {"symbol": symbol.upper(), "limit": 1_000},
                write=False,
            )
            fills.extend(self._fill_snapshot(row) for row in fill_rows)
        balances = []
        for row in account.get("balances", []):
            available = float(row.get("free", 0))
            locked = float(row.get("locked", 0))
            if available == 0 and locked == 0:
                continue
            balances.append(
                VenueBalanceSnapshot(
                    exchange=self.exchange,
                    environment=ExecutionEnvironment.TESTNET,
                    asset=row["asset"],
                    available=available,
                    locked=locked,
                    equity=available + locked,
                    observed_at=observed_at,
                )
            )
        return VenueStateSnapshot(
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            orders=list(orders.values()),
            fills=fills,
            positions=[],
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
            raise SecurityError("Order is not for Binance TESTNET")

    async def _signed_request(
        self,
        method: str,
        path: str,
        params: dict,
        *,
        write: bool,
    ):
        pairs = [
            (key, _number(value) if isinstance(value, float) else str(value))
            for key, value in params.items()
            if value is not None
        ]
        pairs.extend(
            [
                ("recvWindow", str(self._receive_window_ms)),
                ("timestamp", str(self._clock_ms())),
            ]
        )
        encoded = urlencode(pairs)
        signature = hmac.new(
            self._credentials.signing_secret.encode(),
            encoded.encode(),
            hashlib.sha256,
        ).hexdigest()
        signed = [*pairs, ("signature", signature)]
        try:
            response = await self._client.request(
                method,
                path,
                params=signed,
                headers={"X-MBX-APIKEY": self._credentials.key_id},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if write:
                raise AmbiguousExecutionError(
                    "Binance TESTNET write status is unknown",
                    metadata={"error_type": type(exc).__name__},
                ) from exc
            raise ExternalServiceError(
                "Binance TESTNET read failed",
                metadata={"error_type": type(exc).__name__},
            ) from exc
        if response.status_code >= 500 and write:
            raise AmbiguousExecutionError(
                "Binance TESTNET write status is unknown",
                metadata={"http_status": response.status_code},
            )
        if response.status_code >= 400:
            error = (
                ExecutionRejectedError
                if write
                else ExternalServiceError
            )
            raise error(
                "Binance TESTNET request rejected",
                metadata={"http_status": response.status_code},
            )
        return response.json()

    def _order_snapshot(
        self,
        row: dict,
        *,
        fallback: OMSOrder | None = None,
    ) -> VenueOrderSnapshot:
        symbol = row.get("symbol") or (
            fallback.symbol if fallback is not None else None
        )
        side = row.get("side") or (
            fallback.side.value if fallback is not None else None
        )
        client_order_id = (
            row.get("clientOrderId")
            or row.get("origClientOrderId")
            or (fallback.client_order_id if fallback is not None else None)
        )
        venue_order_id = row.get("orderId")
        if not symbol or not side or not client_order_id or venue_order_id is None:
            raise ExternalServiceError(
                "Binance TESTNET returned an incomplete order identity"
            )
        quantity = float(row.get("origQty") or (fallback.quantity if fallback else 0))
        filled = float(row.get("executedQty", 0))
        quote_filled = float(row.get("cummulativeQuoteQty", 0))
        average = quote_filled / filled if filled > 0 and quote_filled > 0 else None
        order_type = OMSOrderType(
            row.get("type") or (fallback.order_type.value if fallback else "MARKET")
        )
        return VenueOrderSnapshot(
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            venue_order_id=str(venue_order_id),
            client_order_id=str(client_order_id),
            symbol=str(symbol),
            side=OrderSide(str(side)),
            order_type=order_type,
            status=_binance_status(str(row.get("status", "NEW"))),
            quantity=quantity,
            cumulative_filled_quantity=filled,
            average_fill_price=average,
        )

    def _fill_snapshot(self, row: dict) -> ExecutionFill:
        return ExecutionFill(
            fill_id=f"binance:{row['symbol']}:{row['id']}",
            venue_order_id=str(row["orderId"]),
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            symbol=row["symbol"],
            side=OrderSide.BUY if row.get("isBuyer") else OrderSide.SELL,
            quantity=float(row["qty"]),
            price=float(row["price"]),
            fee=float(row.get("commission", 0)),
            fee_asset=row.get("commissionAsset"),
            occurred_at=datetime.fromtimestamp(
                int(row["time"]) / 1_000,
                tz=timezone.utc,
            ),
        )


def _number(value) -> str:
    if value is None:
        raise ValueError("Numeric exchange parameter is required")
    return format(float(value), ".16g")


def _binance_time_in_force(value: str) -> str:
    return "GTX" if value == "POST_ONLY" else value


def _binance_status(value: str) -> OMSOrderStatus:
    return {
        "NEW": OMSOrderStatus.SUBMITTED,
        "PENDING_NEW": OMSOrderStatus.SUBMITTED,
        "PARTIALLY_FILLED": OMSOrderStatus.PARTIALLY_FILLED,
        "FILLED": OMSOrderStatus.FILLED,
        "CANCELED": OMSOrderStatus.CANCELED,
        "PENDING_CANCEL": OMSOrderStatus.CANCEL_PENDING,
        "REJECTED": OMSOrderStatus.REJECTED,
        "EXPIRED": OMSOrderStatus.EXPIRED,
        "EXPIRED_IN_MATCH": OMSOrderStatus.EXPIRED,
    }.get(value, OMSOrderStatus.UNKNOWN)
