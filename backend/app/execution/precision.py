"""Deterministic venue quantity normalization using decimal arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from app.core.errors import ExecutionRejectedError, ExternalServiceError
from app.schemas.oms import OMSOrder


@dataclass(frozen=True)
class QuantityRules:
    step: Decimal
    minimum: Decimal
    maximum: Decimal | None = None
    minimum_notional: Decimal | None = None

    @classmethod
    def from_strings(
        cls,
        *,
        step: str,
        minimum: str,
        maximum: str | None = None,
        minimum_notional: str | None = None,
    ) -> "QuantityRules":
        try:
            parsed = cls(
                step=Decimal(step),
                minimum=Decimal(minimum),
                maximum=(
                    Decimal(maximum)
                    if maximum not in (None, "", "0")
                    else None
                ),
                minimum_notional=(
                    Decimal(minimum_notional)
                    if minimum_notional not in (None, "", "0")
                    else None
                ),
            )
        except InvalidOperation as exc:
            raise ExternalServiceError(
                "Venue returned invalid quantity rules"
            ) from exc
        if parsed.step <= 0 or parsed.minimum < 0:
            raise ExternalServiceError(
                "Venue returned unsafe quantity rules"
            )
        return parsed

    def normalize(self, order: OMSOrder) -> OMSOrder:
        requested = Decimal(str(order.quantity))
        normalized = (
            requested / self.step
        ).to_integral_value(rounding=ROUND_DOWN) * self.step
        reference_price = Decimal(str(order.reference_price))
        notional = normalized * reference_price
        if normalized <= 0 or normalized < self.minimum:
            raise ExecutionRejectedError(
                "Order is below the venue minimum quantity"
            )
        if self.maximum is not None and normalized > self.maximum:
            raise ExecutionRejectedError(
                "Order exceeds the venue maximum quantity"
            )
        if (
            self.minimum_notional is not None
            and notional < self.minimum_notional
        ):
            raise ExecutionRejectedError(
                "Order is below the venue minimum notional"
            )
        return OMSOrder.model_validate(
            {
                **order.model_dump(),
                "quantity": float(normalized),
                "requested_notional": float(notional),
            }
        )
