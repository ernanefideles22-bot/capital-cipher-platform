"""Independent evidence for an exchange-triggered stop; never transmits requests."""

import re
from decimal import Decimal
from app.demo.gate_stop_validation import stop_configuration_matches


def confirmed_stop_exit(
    *,
    stop,
    order,
    position,
    inventory,
    stop_id,
    contract,
    signed_position,
    trigger_price,
    stop_observed_at,
    order_observed_at,
    position_observed_at,
    inventory_started_at,
    inventory_finished_at,
    now
):
    try:
        if not stop_configuration_matches(
            stop,
            order_id=stop_id,
            contract=contract,
            position_size=signed_position,
            trigger_price=trigger_price,
        ):
            return False
        raw = (
            stop_observed_at,
            order_observed_at,
            position_observed_at,
            inventory_started_at,
            inventory_finished_at,
            now,
            stop["create_time"],
            stop["finish_time"],
            order["size"],
            order["left"],
            position["size"],
        )
        if any(v is None or isinstance(v, bool) for v in raw):
            return False
        st, ot, pt, begin, end, current, created, finished, size, left, pos = map(
            lambda v: Decimal(str(v)), raw
        )
        if any(
            not v.is_finite()
            for v in (
                st,
                ot,
                pt,
                begin,
                end,
                current,
                created,
                finished,
                size,
                left,
                pos,
            )
        ):
            return False
        generated = str(stop["trade_id"])
        return (
            bool(re.fullmatch(r"[1-9][0-9]*", generated))
            and contract in {"BTC_USDT", "ETH_USDT", "SOL_USDT"}
            and min(st, ot, pt, begin, end, current, created) > 0
            and all(0 <= current - v <= 5 for v in (st, ot, pt, end))
            and 0 <= end - begin <= 5
            and created <= finished <= created + stop["trigger"]["expiration"]
            and finished <= current
            and stop["status"] == "finished"
            and stop["finish_as"] == "succeeded"
            and str(order.get("id_string") or order["id"]) == generated
            and order["contract"] == contract
            and position["contract"] == contract
            and order["status"] == "finished"
            and order["finish_as"] == "filled"
            and order.get("is_close") is True
            and order.get("is_reduce_only") is True
            and order.get("is_liq") is False
            and size == 0
            and left == 0
            and pos == 0
            and position.get("mode") == "single"
            and inventory.get("atomic") is False
            and inventory.get("execution_authorized") is False
            and all(
                type(inventory.get(k)) is int and inventory[k] == 0
                for k in ("positions", "orders", "conditional_orders")
            )
        )
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
        return False
