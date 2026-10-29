"""Gate Testnet contract sizing and client identity; does not submit orders."""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import re


class GateConstraintError(ValueError):
    pass


def _number(value):
    if isinstance(value, bool) or value is None:
        raise GateConstraintError("invalid numeric input")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise GateConstraintError("invalid numeric input") from None
    if not number.is_finite():
        raise GateConstraintError("invalid numeric input")
    return number


def gate_client_id(approval_id):
    """Derive a deterministic venue-compatible id from the complete approval id."""
    if not isinstance(approval_id, str) or not re.fullmatch(r"[a-f0-9]{64}", approval_id):
        raise GateConstraintError("invalid approval identity")
    return "t-" + hashlib.sha256(approval_id.encode("ascii")).hexdigest()[:28]


def prepare_contract_quantity(*, symbol, side, requested_quantity, max_notional,
                              reference_price, metadata, observed_at, now):
    """Return conservative integer contracts using fresh Testnet contract metadata.

    This is a preparation primitive, not an approval or a trading adapter. The
    reference price is not an execution price; live price and risk must be
    checked again immediately before any future submission.
    """
    contracts = {"BTCUSDT": "BTC_USDT", "ETHUSDT": "ETH_USDT", "SOLUSDT": "SOL_USDT"}
    contract = contracts.get(symbol)
    if not contract or side not in {"BUY", "SELL"} or not isinstance(metadata, dict):
        raise GateConstraintError("unsupported contract or side")
    try:
        requested, notional, price, observed, current = map(
            _number, (requested_quantity, max_notional, reference_price, observed_at, now))
        multiplier = _number(metadata["quanto_multiplier"])
        minimum = _number(metadata["order_size_min"])
        maximum = _number(metadata["order_size_max"])
        if metadata["name"] != contract or metadata["in_delisting"] is not False:
            raise GateConstraintError("contract metadata mismatch or delisting")
        if current <= 0 or observed <= 0 or not (0 <= current - observed <= 10):
            raise GateConstraintError("stale contract metadata")
        if min(requested, notional, price, multiplier, minimum, maximum) <= 0 or maximum < minimum:
            raise GateConstraintError("invalid contract limits")
        # Integer contracts only: fractional-contract support is deliberately
        # not used until its step rules are separately verified.
        allowed = min(requested / multiplier, notional / (multiplier * price), maximum, Decimal(1000000))
        size = int(allowed.to_integral_value(rounding=ROUND_FLOOR))
        if size <= 0 or Decimal(size) < minimum:
            raise GateConstraintError("approved quantity below contract minimum")
        base_quantity = Decimal(size) * multiplier
        exposure = base_quantity * price
        if base_quantity > requested or exposure > notional:
            raise GateConstraintError("sizing exceeds approval")
        return {"contract": contract, "signed_contracts": size if side == "BUY" else -size,
                "base_quantity": str(base_quantity), "reference_notional": str(exposure),
                "execution_authorized": False}
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError) as error:
        if isinstance(error, GateConstraintError):
            raise
        raise GateConstraintError("invalid contract metadata") from None
