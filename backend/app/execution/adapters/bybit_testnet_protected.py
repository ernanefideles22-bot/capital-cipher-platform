"""Fail-closed protected Bybit Futures TESTNET execution adapter."""

from __future__ import annotations

from app.core.errors import ExecutionRejectedError, SecurityError
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
)


class ProtectedBybitTestnetExecutionAdapter(
    LegacyBybitTestnetExecutionAdapter
):
    """Require durable risk protection before any Bybit entry write."""

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
