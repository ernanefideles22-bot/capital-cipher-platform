"""Fail-closed protected Bybit Futures TESTNET execution adapter."""

from __future__ import annotations

from app.core.errors import ExecutionRejectedError, ExternalServiceError, SecurityError
from app.execution.adapters.bybit_testnet import (
    BYBIT_TESTNET_BASE_URL,
    BybitTestnetExecutionAdapter as LegacyBybitTestnetExecutionAdapter,
    _bybit_time_in_force,
    _number,
)
from app.execution.bybit_protection import (
    BybitOrderProtection,
    BybitOrderProtectionStore,
)
from app.execution.credentials import TestnetCredentials
from app.schemas.common import OrderSide
from app.schemas.oms import (
    ExecutionEnvironment,
    OMSOrder,
    OMSOrderStatus,
    OMSOrderType,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
)


class ProtectedBybitTestnetExecutionAdapter(
    LegacyBybitTestnetExecutionAdapter
):
    """Require durable risk protection before any Bybit TESTNET write."""

    def __init__(
        self,
        credentials: TestnetCredentials,
        *,
        base_url: str = BYBIT_TESTNET_BASE_URL,
        category: str = "linear",
        timeout_seconds: float = 5.0,
        receive_window_ms: int = 5_000,
        client=None,
        clock_ms=None,
        protection_store=None,
    ) -> None:
        super().__init__(
            credentials,
            base_url=base_url,
            category=category,
            timeout_seconds=timeout_seconds,
            receive_window_ms=receive_window_ms,
            client=client,
            clock_ms=clock_ms,
        )
        self._protection_store = protection_store
        self._owns_protection_store = protection_store is None

    async def submit_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        self._validate_order(order)
        protection = await self._load_protection(order)
        self._validate_entry_protection(order, protection)
        await self._configure_leverage(order, protection)

        body: dict[str, object] = {
            "category": self._category,
            "symbol": order.symbol.upper(),
            "side": "Buy" if order.side == OrderSide.BUY else "Sell",
            "orderType": (
                "Market"
                if order.order_type == OMSOrderType.MARKET
                else "Limit"
            ),
            "qty": _number(order.quantity),
            "timeInForce": _bybit_time_in_force(order.time_in_force.value),
            "positionIdx": 0,
            "orderLinkId": order.client_order_id,
            "reduceOnly": False,
            "takeProfit": _number(protection.take_profit),
            "stopLoss": _number(protection.stop_loss),
            "tpslMode": "Full",
            "tpOrderType": "Market",
            "slOrderType": "Market",
            "tpTriggerBy": "MarkPrice",
            "slTriggerBy": "MarkPrice",
        }
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
            client_order_id=(
                result.get("orderLinkId") or order.client_order_id
            ),
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            status=OMSOrderStatus.SUBMITTED,
            quantity=order.quantity,
        )

    async def fetch_account_equity(self, asset: str = "USDT") -> float:
        """Return venue equity for sizing; fail closed on missing/invalid data."""

        normalized_asset = asset.strip().upper()
        if normalized_asset != "USDT":
            raise SecurityError(
                "Bybit TESTNET risk accounting currently supports only USDT equity"
            )
        payload = await self._request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED", "coin": normalized_asset},
            write=False,
        )
        matching: list[dict] = []
        for account in payload.get("result", {}).get("list", []):
            for coin in account.get("coin", []):
                if str(coin.get("coin", "")).upper() == normalized_asset:
                    matching.append(coin)
        if len(matching) != 1:
            raise ExternalServiceError(
                "Bybit TESTNET returned ambiguous USDT equity"
            )
        try:
            equity = float(matching[0].get("equity"))
        except (TypeError, ValueError) as exc:
            raise ExternalServiceError(
                "Bybit TESTNET returned invalid USDT equity"
            ) from exc
        if equity <= 0:
            raise SecurityError(
                "Bybit TESTNET equity must be positive before risk sizing"
            )
        return equity

    async def submit_reduce_only_exit(
        self,
        position: VenuePositionSnapshot,
        *,
        client_order_id: str,
    ) -> VenueOrderSnapshot:
        """Close one reconciled one-way position without increasing exposure."""

        if (
            position.exchange != self.exchange
            or position.environment != ExecutionEnvironment.TESTNET
        ):
            raise SecurityError("Reduce-only exit is not for Bybit TESTNET")
        if position.quantity <= 0:
            raise SecurityError("Reduce-only exit requires positive position quantity")
        normalized_client_id = client_order_id.strip()
        if not 8 <= len(normalized_client_id) <= 36:
            raise SecurityError("Reduce-only exit requires a bounded client order id")

        close_side = (
            OrderSide.SELL
            if position.side == OrderSide.BUY
            else OrderSide.BUY
        )
        body: dict[str, object] = {
            "category": self._category,
            "symbol": position.symbol.upper(),
            "side": "Buy" if close_side == OrderSide.BUY else "Sell",
            "orderType": "Market",
            "qty": _number(position.quantity),
            "timeInForce": "IOC",
            "positionIdx": 0,
            "orderLinkId": normalized_client_id,
            "reduceOnly": True,
        }
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
            client_order_id=(
                result.get("orderLinkId") or normalized_client_id
            ),
            symbol=position.symbol,
            side=close_side,
            order_type=OMSOrderType.MARKET,
            status=OMSOrderStatus.SUBMITTED,
            quantity=position.quantity,
        )

    async def _load_protection(
        self,
        order: OMSOrder,
    ) -> BybitOrderProtection:
        if self._protection_store is None:
            self._protection_store = (
                BybitOrderProtectionStore.from_environment()
            )
        protection = await self._protection_store.load(order.oms_order_id)
        if protection is None:
            raise SecurityError(
                "Bybit TESTNET order has no durable protection evidence"
            )
        return protection

    @staticmethod
    def _validate_entry_protection(
        order: OMSOrder,
        protection: BybitOrderProtection,
    ) -> None:
        if (
            protection.oms_order_id != order.oms_order_id
            or protection.approval_id != order.approval_id
            or protection.exchange != order.exchange.value
            or protection.environment != order.environment.value
        ):
            raise SecurityError(
                "Bybit TESTNET protection identity does not match OMS order"
            )
        if protection.execution_intent != "ENTRY" or protection.reduce_only:
            raise SecurityError(
                "Bybit TESTNET discretionary exits are not released"
            )
        if abs(protection.leverage - order.leverage) > 1e-12:
            raise SecurityError(
                "Bybit TESTNET leverage differs from central-risk approval"
            )
        if protection.stop_loss <= 0 or protection.take_profit <= 0:
            raise SecurityError(
                "Bybit TESTNET entry requires positive stop loss and take profit"
            )
        if order.side == OrderSide.BUY and not (
            protection.stop_loss
            < order.reference_price
            < protection.take_profit
        ):
            raise SecurityError(
                "Bybit BUY protection must bracket the approved entry price"
            )
        if order.side == OrderSide.SELL and not (
            protection.take_profit
            < order.reference_price
            < protection.stop_loss
        ):
            raise SecurityError(
                "Bybit SELL protection must bracket the approved entry price"
            )

    async def _configure_leverage(
        self,
        order: OMSOrder,
        protection: BybitOrderProtection,
    ) -> None:
        leverage = _number(protection.leverage)
        try:
            await self._request(
                "POST",
                "/v5/position/set-leverage",
                body={
                    "category": self._category,
                    "symbol": order.symbol.upper(),
                    "buyLeverage": leverage,
                    "sellLeverage": leverage,
                },
                write=True,
            )
        except ExecutionRejectedError as exc:
            # Bybit retCode 110043 means the requested leverage already
            # matches the current setting, so the idempotent precondition holds.
            if exc.metadata.get("venue_code") != 110043:
                raise

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            if (
                self._owns_protection_store
                and self._protection_store is not None
            ):
                await self._protection_store.aclose()
