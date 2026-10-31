"""Protected Gate TESTNET adapter. Not registered in the application runtime.

The caller supplies an exclusive account lease/central-risk guard and ONE shared,
crash-safe journal on a durable volume. A halted cycle is never reset here.
Construction does not enable trading; both this adapter and transport deny writes
by default. No production host, credentials, or environment is accepted.
"""

from datetime import datetime, timezone
from decimal import Decimal
import asyncio
import hashlib
import logging

from app.core.errors import SecurityError, AmbiguousExecutionError, ExternalServiceError
from app.demo.gate_order_plan import build_order_plan
from app.demo.gate_position_config import interpret_position_config
from app.demo.gate_stop_validation import stop_matches
from app.demo.gate_exit_evidence import confirmed_close_order
from app.execution.adapters.gate_testnet_preparation import (
    GateTestnetPreparationAdapter,
)
from app.execution.adapters.gate_order_constraints import (
    gate_client_id,
    prepare_contract_quantity,
)
from app.schemas.common import Exchange, OrderSide
from app.schemas.oms import (
    ExecutionEnvironment,
    OMSOrderStatus,
    OMSOrderType,
    VenueOrderSnapshot,
    VenueStateSnapshot,
    VenuePositionSnapshot,
    VenueBalanceSnapshot,
    ExecutionFill,
)

CONTRACTS = {"BTCUSDT": "BTC_USDT", "ETHUSDT": "ETH_USDT", "SOLUSDT": "SOL_USDT"}


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("invalid number")
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("nonfinite number")
    return value


class GateTestnetExecutionAdapter(GateTestnetPreparationAdapter):
    def __init__(
        self,
        metadata_provider,
        clock,
        *,
        transport,
        journal,
        protection_store,
        account_guard,
        execution_enabled=False,
        exit_guard=None,
        entry_order_guard=None,
    ):
        super().__init__(metadata_provider, clock)
        if not callable(account_guard):
            raise ValueError("exclusive account and central-risk guard required")
        self._transport = transport
        self._journal = journal
        self._protection = protection_store
        self._guard = account_guard
        self._exit_guard = exit_guard or account_guard
        self._enabled = execution_enabled is True
        self._entry_order_guard = entry_order_guard
        self._lock = asyncio.Lock()

    async def _authorize(self, *, exit_only=False):
        guard = self._exit_guard if exit_only else self._guard
        if not self._enabled or await guard() is not True:
            raise SecurityError(
                "Gate execution disabled or central controls unavailable"
            )

    def _scope(self, order):
        if (
            order.exchange != Exchange.GATE
            or order.environment != ExecutionEnvironment.TESTNET
        ):
            raise SecurityError("Gate adapter only accepts Gate TESTNET orders")

    async def healthcheck(self):
        try:
            await self._authorize()
            state = await self.fetch_state()
            return bool(state.balances and state.balances[0].equity > 0)
        except Exception:
            return False

    async def _plan(self, order):
        prepared = await super().prepare_order(order)
        if (
            prepared.client_order_id != order.client_order_id
            or number(prepared.quantity) != number(order.quantity)
            or number(order.leverage) != 1
        ):
            raise SecurityError(
                "OMS order must already match Gate preparation and 1x risk approval"
            )
        proof = await self._protection.verify(order)
        if proof.get("verified") is not True:
            raise SecurityError("durable protection not verified")
        metadata, observed = await self._metadata_provider(order.symbol)
        qty = prepare_contract_quantity(
            symbol=order.symbol,
            side=order.side.value,
            requested_quantity=order.quantity,
            max_notional=order.requested_notional,
            reference_price=order.reference_price,
            metadata=metadata,
            observed_at=observed,
            now=self._clock(),
        )
        if number(qty["base_quantity"]) != number(order.quantity):
            raise SecurityError("contract sizing changed after approval")
        mark = number(metadata["mark_price"])
        stop, target = number(proof["stop_loss"]), number(proof["take_profit"])
        if (
            mark <= 0
            or number(order.quantity) * mark > number(order.requested_notional)
            or not (
                stop < mark < target
                if order.side == OrderSide.BUY
                else target < mark < stop
            )
        ):
            raise SecurityError("current market price exceeds approved entry bounds")
        # The Gate trigger endpoint rejects rule=2 unless trigger.price is
        # below the venue's *last traded* price (rule=1 requires above),
        # regardless of price_type=1 (mark-price trigger evaluation).
        # Never trade when the upstream quote is unavailable or too close.
        tick = number(metadata["order_price_round"])
        last = number(metadata.get("last_price"))
        if not (stop < last - tick if order.side == OrderSide.BUY else stop > last + tick):
            raise SecurityError("Gate stop cannot satisfy venue last-price rule before entry")
        plan = build_order_plan(
            contract=qty["contract"],
            signed_contracts=qty["signed_contracts"],
            mark_price=mark,
            stop_price=proof["stop_loss"],
            tick_size=metadata["order_price_round"],
            client_id=order.client_order_id,
        )
        return plan, metadata, proof, observed

    async def _inventory(self):
        started = self._clock()
        positions = await self._transport.rows("positions")
        orders = await self._transport.rows("orders", status="open")
        stops = await self._transport.rows("stops", status="open")
        finished = self._clock()
        if not 0 <= number(finished) - number(started) <= 5:
            raise ExternalServiceError("Gate inventory too old")
        if any(row.get("status") != "open" for row in orders + stops):
            raise ExternalServiceError("invalid open-order inventory")
        counts = {
            "positions": sum(number(row["size"]) != 0 for row in positions),
            "orders": len(orders),
            "conditional_orders": len(stops),
            "atomic": False,
            "execution_authorized": False,
        }
        return counts, started, finished, positions, orders, stops

    async def submit_order(self, order):
        async with self._lock:
            await self._authorize()
            if self._entry_order_guard is not None:
                await self._entry_order_guard(order)
            plan, metadata, proof, observed = await self._plan(order)
            contract = plan["entry"]["contract"]
            counts, *_ = await self._inventory()
            config = await self._transport.get_position(contract)
            # Same one-way/isolated interpretation is applied to all supported contracts.
            mode = interpret_position_config(config, contract=contract)
            if (
                config.get("contract") != contract
                or not mode["one_way_isolated_1x"]
                or mode["position_open"]
            ):
                raise SecurityError("Gate account must be flat, one-way isolated 1x")
            if any(counts[k] for k in ("positions", "orders", "conditional_orders")):
                raise SecurityError(
                    "Gate account contains unresolved exposure or orders"
                )
            account = await self._transport.account()
            if (
                account.get("currency") != "USDT"
                or account.get("in_dual_mode") is not False
                or account.get("enable_credit") is True
                or number(account["available"])
                < number(order.quantity) * number(metadata["mark_price"])
            ):
                raise SecurityError(
                    "Gate settlement, account mode or available margin invalid"
                )
            if not 0 <= number(self._clock()) - number(observed) <= 10:
                raise SecurityError("entry metadata expired during preflight")
            cycle = order.oms_order_id
            self._journal.create(
                cycle, contract, plan["stop"]["trigger"]["price"], plan["entry"]["size"]
            )
            self._journal.record_bound_entry_intent(cycle, order.client_order_id)
            try:
                await self._authorize()
                if self._entry_order_guard is not None:
                    await self._entry_order_guard(order)
                if not 0 <= number(self._clock()) - number(observed) <= 10:
                    raise SecurityError("entry metadata expired before dispatch")
                ack = await self._transport.place_entry(
                    contract=contract,
                    signed_contracts=plan["entry"]["size"],
                    client_id=order.client_order_id,
                )
                self._journal.record_entry_acknowledgement(cycle, ack["id"])
                entry = await self._transport.get_order(ack["id"])
                entry_time = self._clock()
                position = await self._transport.get_position(contract)
                position_time = self._clock()
                if (
                    entry.get("status") == "finished"
                    and entry.get("finish_as") in {"ioc", "cancelled"}
                    and abs(number(entry["left"])) == abs(number(entry["size"]))
                ):
                    counts, started, finished, *_ = await self._inventory()
                    snapshot = self._order_snapshot(entry, order.symbol, metadata)
                    if not self._journal.record_verified_no_fill(
                        cycle,
                        order=entry,
                        position=position,
                        inventory=counts,
                        order_observed_at=entry_time,
                        position_observed_at=position_time,
                        inventory_started_at=started,
                        inventory_finished_at=finished,
                        now=self._clock(),
                    ):
                        raise ValueError("zero fill not independently reconciled")
                    self._journal.cache_terminal_entry(cycle, snapshot)
                    return snapshot
                if not self._journal.record_verified_entry(
                    cycle,
                    order=entry,
                    position=position,
                    order_observed_at=entry_time,
                    position_observed_at=position_time,
                    now=self._clock(),
                ):
                    raise ValueError("entry not reconciled")
                # Protection must still bracket the ACTUAL fill, not just the reference price.
                filled = int(self._journal.read(cycle)["position"])
                fill_price = number(entry["fill_price"])
                stop_price, target = number(plan["stop"]["trigger"]["price"]), number(
                    proof["take_profit"]
                )
                if not (
                    stop_price < fill_price < target
                    if filled > 0
                    else target < fill_price < stop_price
                ) or abs(filled) * number(
                    metadata["quanto_multiplier"]
                ) * fill_price > number(
                    order.requested_notional
                ):
                    raise ValueError("fill crossed approved price or notional bounds")
                # Refresh the venue's last traded price immediately before
                # sending any price-triggered stop. A mark/last divergence or
                # adverse move during entry can invalidate the stop price even
                # when it was valid against the pre-entry mark price.
                latest, last_observed = await self._metadata_provider(order.symbol)
                last_trade = number(latest.get("last_price"))
                last_tick = number(latest["order_price_round"])
                if (
                    latest.get("name") != contract
                    or last_tick != number(metadata["order_price_round"])
                    or not 0 <= number(self._clock()) - number(last_observed) <= 10
                    or not (stop_price < last_trade - last_tick if filled > 0
                            else stop_price > last_trade + last_tick)
                ):
                    raise ValueError("Gate stop no longer valid against latest traded price")
                self._journal.record_stop_intent(cycle)
                stop = await self._transport.place_stop(
                    contract=contract,
                    signed_position=filled,
                    reference_price=str(fill_price),
                    stop_price=plan["stop"]["trigger"]["price"],
                    tick_size=metadata["order_price_round"],
                )
                self._journal.record_stop_acknowledgement(cycle, stop_id=stop["id"])
                snapshot = await self._transport.get_stop(stop["id"])
                stop_time = self._clock()
                position = await self._transport.get_position(contract)
                if (
                    position.get("contract") != contract
                    or position.get("mode") != "single"
                    or number(position["size"]) != filled
                    or not stop_matches(
                        snapshot,
                        order_id=stop["id"],
                        contract=contract,
                        position_size=filled,
                        trigger_price=plan["stop"]["trigger"]["price"],
                        observed_at=stop_time,
                        now=self._clock(),
                    )
                ):
                    raise ValueError("position or stop not independently confirmed")
                if not self._journal.record_stop_confirmation(
                    cycle,
                    snapshot=snapshot,
                    observed_at=stop_time,
                    now=self._clock(),
                    confirmed_position=position["size"],
                ):
                    raise ValueError("stop not confirmed")
                snapshot = self._order_snapshot(entry, order.symbol, metadata)
                self._journal.cache_terminal_entry(cycle, snapshot)
                return snapshot
            except BaseException as exc:
                # Never log provider text, payloads, credentials or exception messages.
                # Preserve the failing stage before emergency close changes it.
                logging.getLogger('uvicorn.error').warning(
                    'GATE_CYCLE_FAILED order_id=%s stage=%s error_type=%s http_status=%s provider_label=%s provider_fields=%s response_kind=%s',
                    cycle, self._journal.read(cycle)['state'], type(exc).__name__,
                    getattr(exc, 'http_status', None), getattr(exc, 'provider_label', None),
                    getattr(exc, 'provider_fields', ()), getattr(exc, 'response_kind', None),
                )
                if isinstance(exc, Exception) and self._journal.read(cycle)[
                    "state"
                ] in {"STOP_REQUIRED", "STOP_SUBMITTING", "STOP_PENDING"}:
                    await self._emergency_close(cycle)
                if self._journal.read(cycle)["state"] not in {
                    "HALTED",
                    "PROTECTED",
                    "CLOSED",
                }:
                    self._journal.halt_unknown(cycle)
                if not isinstance(exc, Exception):
                    raise
                raise AmbiguousExecutionError(
                    "Gate cycle unresolved; reconcile without resubmitting"
                ) from None

    async def _emergency_close(self, cycle):
        """One owned reduce-only attempt; never clear a halt or retry a write."""
        row = self._journal.read(cycle)
        identity = self._journal.db.execute(
            "SELECT order_id FROM entry_identity WHERE cycle_id=?", (cycle,)
        ).fetchone()
        try:
            entry = await self._transport.get_order(identity[0])
            entry_time = self._clock()
            position = await self._transport.get_position(row["contract"])
            pos_time = self._clock()
            client_id = (
                "t-" + hashlib.sha256(("emergency:" + cycle).encode()).hexdigest()[:28]
            )
            self._journal.record_bound_emergency_exit_intent(
                cycle,
                client_id=client_id,
                order=entry,
                position=position,
                order_observed_at=entry_time,
                position_observed_at=pos_time,
                now=self._clock(),
            )
            ack = await self._transport.close_position(
                contract=row["contract"], client_id=client_id
            )
            self._journal.record_exit_acknowledgement(cycle, ack["id"])
            close = await self._transport.get_order(ack["id"])
            close_time = self._clock()
            position = await self._transport.get_position(row["contract"])
            pos_time = self._clock()
            self._journal.record_emergency_close_observation(
                cycle,
                order=close,
                position=position,
                order_observed_at=close_time,
                position_observed_at=pos_time,
                now=self._clock(),
            )
        except BaseException as exc:
            if self._journal.read(cycle)["state"] != "HALTED":
                self._journal.halt_unknown(cycle)
            if not isinstance(exc, Exception):
                raise

    def _order_snapshot(self, row, symbol, metadata):
        size, left, multiplier = (
            number(row["size"]),
            number(row["left"]),
            number(metadata["quanto_multiplier"]),
        )
        if (
            row.get("contract") != CONTRACTS[symbol]
            or multiplier <= 0
            or size == 0
            or abs(left) > abs(size)
            or size != size.to_integral_value()
            or left != left.to_integral_value()
        ):
            raise ValueError("invalid venue order")
        filled = (abs(size) - abs(left)) * multiplier
        if row["status"] == "open":
            status = (
                OMSOrderStatus.PARTIALLY_FILLED if filled else OMSOrderStatus.SUBMITTED
            )
        elif row["status"] == "finished" and row["finish_as"] == "filled" and left == 0:
            status = OMSOrderStatus.FILLED
        elif row["status"] == "finished" and row["finish_as"] in {"cancelled", "ioc"}:
            status = OMSOrderStatus.CANCELED
        else:
            raise ValueError("unrecognized venue status")
        return VenueOrderSnapshot(
            exchange=Exchange.GATE,
            venue_order_id=str(row.get("id_string") or row["id"]),
            client_order_id=row["text"],
            symbol=symbol,
            side=OrderSide.BUY if size > 0 else OrderSide.SELL,
            order_type=OMSOrderType.MARKET,
            status=status,
            quantity=float(abs(size) * multiplier),
            cumulative_filled_quantity=float(filled),
            average_fill_price=float(number(row["fill_price"])) if filled else None,
        )

    async def cancel_order(self, order):
        async with self._lock:
            return await self._cancel_order(order)

    async def _cancel_order(self, order):
        # Only IOC market entries are supported. Never cancel a protective stop
        # through an OMS entry-cancellation command or interpret cancel as close.
        await self._authorize()
        self._scope(order)
        row = self._journal.read(order.oms_order_id)
        if row["state"] != "PROTECTED":
            raise SecurityError(
                "unresolved entry cannot be canceled without reconciliation"
            )
        identity = self._journal.db.execute(
            "SELECT client_id,order_id FROM entry_identity WHERE cycle_id=?",
            (order.oms_order_id,),
        ).fetchone()
        if identity is None or identity[0] != order.client_order_id or not identity[1]:
            raise SecurityError("entry ownership missing")
        venue = await self._transport.get_order(identity[1])
        if venue.get("text") != identity[0] or venue.get("status") != "finished":
            raise SecurityError("IOC entry not terminal; no blind cancellation")
        metadata, _ = await self._metadata_provider(order.symbol)
        return self._order_snapshot(venue, order.symbol, metadata)

    async def close_protected_order(self, order):
        """Close only this reconciled protected cycle; never clear an old halt."""
        async with self._lock:
            await self._authorize(exit_only=True)
            self._scope(order)
            cycle = order.oms_order_id
            row = self._journal.read(cycle)
            if row["state"] != "PROTECTED" or row["contract"] != CONTRACTS.get(
                order.symbol
            ):
                raise SecurityError("cycle not protected or wrong scope")
            identity = self._journal.db.execute(
                "SELECT client_id,order_id FROM entry_identity WHERE cycle_id=?",
                (cycle,),
            ).fetchone()
            if (
                not identity
                or identity[0] != gate_client_id(order.approval_id)
                or identity[0] != order.client_order_id
            ):
                raise SecurityError("entry ownership mismatch")
            position = await self._transport.get_position(row["contract"])
            pos_time = self._clock()
            stop = await self._transport.get_stop(row["stop_id"])
            stop_time = self._clock()
            now = self._clock()
            if (
                position.get("contract") != row["contract"]
                or position.get("mode") != "single"
                or not 0 <= number(now) - number(pos_time) <= 5
                or number(position["size"]) != number(row["position"])
                or not stop_matches(
                    stop,
                    order_id=row["stop_id"],
                    contract=row["contract"],
                    position_size=row["position"],
                    trigger_price=row["target"],
                    observed_at=stop_time,
                    now=now,
                )
            ):
                raise SecurityError("position or protection changed before close")
            exit_id = "t-" + hashlib.sha256(("exit:" + cycle).encode()).hexdigest()[:28]
            self._journal.record_bound_exit_intent(cycle, exit_id)
            try:
                ack = await self._transport.close_position(
                    contract=row["contract"], client_id=exit_id
                )
                self._journal.record_exit_acknowledgement(cycle, ack["id"])
                venue = await self._transport.get_order(ack["id"])
                order_time = self._clock()
                position = await self._transport.get_position(row["contract"])
                pos_time = self._clock()
                # Do not remove protection until the exchange confirms the close.
                # Validate bound identity and scope before removing protection.
                # Actual account inventory is required again for CLOSED below.
                if not confirmed_close_order(
                    order=venue,
                    position=position,
                    contract=row["contract"],
                    order_id=ack["id"],
                    client_id=exit_id,
                    order_observed_at=order_time,
                    position_observed_at=pos_time,
                    now=self._clock(),
                    expected_signed_position=row['position'],
                ):
                    raise ValueError("close not confirmed")
                await self._transport.cancel_stop(row["stop_id"])
                counts, started, finished, *_ = await self._inventory()
                # Refresh after cancellation so the complete evidence fits the freshness window.
                venue = await self._transport.get_order(ack["id"])
                order_time = self._clock()
                position = await self._transport.get_position(row["contract"])
                pos_time = self._clock()
                if not self._journal.record_verified_exit(
                    cycle,
                    order=venue,
                    position=position,
                    inventory=counts,
                    order_observed_at=order_time,
                    position_observed_at=pos_time,
                    inventory_started_at=started,
                    inventory_finished_at=finished,
                    now=self._clock(),
                ):
                    raise ValueError("flat account not confirmed")
                return {
                    "state": "CLOSED",
                    "venue_order_id": ack["id"],
                    "client_order_id": exit_id,
                }
            except BaseException as exc:
                if self._journal.read(cycle)["state"] != "HALTED":
                    self._journal.halt_unknown(cycle)
                if not isinstance(exc, Exception):
                    raise
                raise AmbiguousExecutionError(
                    "Gate close unresolved; do not retry"
                ) from None

    async def capture_canary_protection(self, order):
        """Fresh signed responses for a reserved canary; read-only, not approval."""
        async with self._lock:
            self._scope(order)
            row = self._journal.read(order.oms_order_id)
            if row["state"] != "PROTECTED":
                raise SecurityError("Canary is not protected")
            identity = self._journal.db.execute(
                "SELECT client_id,order_id FROM entry_identity WHERE cycle_id=?", (row["id"],)
            ).fetchone()
            if not identity or identity[0] != order.client_order_id:
                raise SecurityError("Canary entry identity unavailable")
            entry = await self._transport.get_order(identity[1])
            entry_time = self._clock()
            stop = await self._transport.get_stop(row["stop_id"])
            stop_time = self._clock()
            position = await self._transport.get_position(row["contract"])
            return dict(state="PROTECTED", order_id=row["id"], entry_order_id=identity[1],
                        stop_id=row["stop_id"], entry=entry, stop=stop, position=position,
                        entry_observed_at=entry_time, stop_observed_at=stop_time,
                        position_observed_at=self._clock())

    async def capture_canary_close(self, order):
        """Fresh signed terminal close and complete flat inventory, no writes."""
        async with self._lock:
            self._scope(order)
            row = self._journal.read(order.oms_order_id)
            if row["state"] != "CLOSED":
                raise SecurityError("Canary is not closed")
            entry = self._journal.db.execute("SELECT order_id FROM entry_identity WHERE cycle_id=?", (row["id"],)).fetchone()
            exit_identity = self._journal.db.execute("SELECT order_id FROM exit_identity WHERE cycle_id=?", (row["id"],)).fetchone()
            if not entry or not exit_identity:
                raise SecurityError("Owned canary close identity unavailable")
            counts, started, finished, *_ = await self._inventory()
            close = await self._transport.get_order(exit_identity[0])
            close_time = self._clock()
            position = await self._transport.get_position(row["contract"])
            return dict(state="CLOSED", order_id=row["id"], entry_order_id=entry[0], stop_id=row["stop_id"],
                        exit=close, position=position, inventory=counts,
                        exit_observed_at=close_time, position_observed_at=self._clock(),
                        inventory_started_at=started, inventory_finished_at=finished)

    async def fetch_account_equity(self):
        account = await self._transport.account()
        if account.get("currency") != "USDT":
            raise SecurityError("Gate settlement mismatch")
        equity = number(account["total"]) + number(account["unrealised_pnl"])
        if equity <= 0:
            raise SecurityError("Gate virtual equity unavailable")
        return float(equity)

    async def reconcile_protected_order(self, order):
        """Prove an exchange-triggered exit by its conditional-order linkage."""
        async with self._lock:
            self._scope(order)
            row = self._journal.read(order.oms_order_id)
            if row["state"] != "PROTECTED" or row["contract"] != CONTRACTS.get(
                order.symbol
            ):
                raise SecurityError("Gate cycle requires operator reconciliation")
            stop = await self._transport.get_stop(row["stop_id"])
            stop_time = self._clock()
            position = await self._transport.get_position(row["contract"])
            if stop.get("status") == "open":
                if (
                    position.get("contract") != row["contract"]
                    or position.get("mode") != "single"
                    or number(position["size"]) != number(row["position"])
                    or not stop_matches(
                        stop,
                        order_id=row["stop_id"],
                        contract=row["contract"],
                        position_size=row["position"],
                        trigger_price=row["target"],
                        observed_at=stop_time,
                        now=self._clock(),
                    )
                ):
                    self._journal.halt_protection(row["id"])
                    raise SecurityError("Gate live stop or position no longer verified")
                return {
                    "state": "PROTECTED",
                    "expires_at": float(
                        number(stop["create_time"])
                        + number(stop["trigger"]["expiration"])
                    ),
                }
            generated = str(stop.get("trade_id") or "")
            if (
                stop.get("status") != "finished"
                or stop.get("finish_as") != "succeeded"
                or not generated.isdecimal()
                or int(generated) <= 0
            ):
                self._journal.halt_protection(row["id"])
                raise SecurityError("Gate stop ended without a proven exit")
            close = await self._transport.get_order(generated)
            close_time = self._clock()
            counts, started, finished, *_ = await self._inventory()
            position = await self._transport.get_position(row["contract"])
            pos_time = self._clock()
            if not self._journal.record_verified_stop_exit(
                row["id"],
                stop=stop,
                order=close,
                position=position,
                inventory=counts,
                stop_observed_at=stop_time,
                order_observed_at=close_time,
                position_observed_at=pos_time,
                inventory_started_at=started,
                inventory_finished_at=finished,
                now=self._clock(),
            ):
                raise SecurityError("Gate generated stop exit not reconciled")
            return {"state": "CLOSED", "venue_order_id": generated, "cause": "STOP"}

    async def fetch_state(self, *, symbols=None):
        async with self._lock:
            return await self._fetch_state(symbols=symbols)

    async def _fetch_state(self, *, symbols=None):
        """Bounded reconciliation of account inventory, fills and bound orders.

        A full API page fails closed rather than claiming history completeness.
        Conditional protection is verified against the journal separately.
        """
        selected = set(CONTRACTS) if not symbols else set(symbols)
        if not selected <= CONTRACTS.keys():
            raise SecurityError("unsupported Gate reconciliation scope")
        try:
            counts, _, observed, positions, opens, stops = await self._inventory()
            allowed_contracts = {CONTRACTS[s] for s in selected}
            if any(
                p.get("contract") not in allowed_contracts and number(p["size"]) != 0
                for p in positions
            ) or any(o.get("contract") not in allowed_contracts for o in opens):
                raise ValueError("exposure outside reconciliation scope")
            known_stops = {
                r[0]
                for r in self._journal.db.execute(
                    "SELECT stop_id FROM cycles WHERE stop_id IS NOT NULL"
                )
            }
            if any(
                str(s.get("id_string") or s.get("id")) not in known_stops for s in stops
            ):
                raise ValueError("orphan conditional order")
            for cycle in self._journal.recovery_status()["cycles"]:
                row = self._journal.read(cycle["id"])
                if row["state"] != "PROTECTED":
                    raise ValueError("unresolved journal cycle")
                matches = [
                    s
                    for s in stops
                    if str(s.get("id_string") or s.get("id")) == row["stop_id"]
                ]
                pos = [p for p in positions if p.get("contract") == row["contract"]]
                if (
                    len(matches) != 1
                    or len(pos) != 1
                    or pos[0].get("mode") != "single"
                    or number(pos[0]["size"]) != number(row["position"])
                    or not stop_matches(
                        matches[0],
                        order_id=row["stop_id"],
                        contract=row["contract"],
                        position_size=row["position"],
                        trigger_price=row["target"],
                        observed_at=observed,
                        now=self._clock(),
                    )
                ):
                    self._journal.halt_protection(row["id"])
                    raise ValueError("active protection or position changed")
            owned_contracts = {
                c["contract"]
                for c in self._journal.recovery_status()["cycles"]
                if c["state"] == "PROTECTED"
            }
            if any(
                number(p["size"]) != 0 and p.get("contract") not in owned_contracts
                for p in positions
            ):
                raise ValueError("orphan Gate position")
            account = await self._transport.account()
            if account["currency"] != "USDT":
                raise ValueError("wrong settlement")
            result = VenueStateSnapshot(
                exchange=Exchange.GATE, environment=ExecutionEnvironment.TESTNET
            )
            for symbol in selected:
                metadata, metadata_time = await self._metadata_provider(symbol)
                multiplier = number(metadata["quanto_multiplier"])
                if (
                    metadata.get("name") != CONTRACTS[symbol]
                    or multiplier <= 0
                    or not 0 <= number(self._clock()) - number(metadata_time) <= 10
                ):
                    raise ValueError("invalid or stale contract multiplier")
                rows = {
                    str(r.get("id_string") or r["id"]): r
                    for r in opens
                    if r["contract"] == CONTRACTS[symbol]
                }
                for oid, cycle_state, archived in self._journal.db.execute(
                    "SELECT e.order_id,c.state,s.payload FROM entry_identity e JOIN cycles c ON c.id=e.cycle_id "
                    "LEFT JOIN terminal_entry_snapshots s ON s.cycle_id=c.id "
                    "WHERE c.contract=? AND e.order_id IS NOT NULL",
                    (CONTRACTS[symbol],),
                ):
                    if cycle_state == "CLOSED" and archived is not None:
                        # Terminal evidence keeps its original observation time.
                        # It is never used as fresh protection/position evidence.
                        if oid in rows:
                            raise ValueError("closed cycle reappeared as open")
                        result.orders.append(
                            VenueOrderSnapshot.model_validate_json(archived)
                        )
                        continue
                    rows[oid] = await self._transport.get_order(oid)
                result.orders.extend(
                    self._order_snapshot(r, symbol, metadata) for r in rows.values()
                )
                for p in positions:
                    if p["contract"] != CONTRACTS[symbol]:
                        continue
                    size = number(p["size"])
                    if size != size.to_integral_value():
                        raise ValueError("fractional contracts unsupported")
                    if p["mode"] != "single":
                        raise ValueError("hedge mode unsupported")
                    result.positions.append(
                        VenuePositionSnapshot(
                            exchange=Exchange.GATE,
                            environment=ExecutionEnvironment.TESTNET,
                            symbol=symbol,
                            side=OrderSide.BUY if size >= 0 else OrderSide.SELL,
                            quantity=float(abs(size) * multiplier),
                            entry_price=float(number(p["entry_price"])),
                            mark_price=float(number(p["mark_price"])),
                            unrealized_pnl=float(number(p["unrealised_pnl"])),
                        )
                    )
                managed_fills = {
                    str(oid): cycle
                    for cycle, oid in self._journal.db.execute(
                        "SELECT e.cycle_id,e.order_id FROM entry_identity e JOIN cycles c ON c.id=e.cycle_id WHERE c.contract=? "
                        "UNION ALL SELECT e.cycle_id,e.order_id FROM exit_identity e JOIN cycles c ON c.id=e.cycle_id WHERE c.contract=? "
                        "UNION ALL SELECT e.cycle_id,e.order_id FROM stop_exit_identity e JOIN cycles c ON c.id=e.cycle_id WHERE c.contract=?",
                        (CONTRACTS[symbol],) * 3,
                    )
                    if oid is not None
                }
                for f in await self._transport.rows(
                    "fills", contract=CONTRACTS[symbol]
                ):
                    size = number(f["size"])
                    if (
                        f["contract"] != CONTRACTS[symbol]
                        or size == 0
                        or size != size.to_integral_value()
                    ):
                        raise ValueError("invalid fill")
                    result.fills.append(
                        ExecutionFill(
                            fill_id=str(f["id"]),
                            oms_order_id=managed_fills.get(str(f["order_id"])),
                            venue_order_id=str(f["order_id"]),
                            client_order_id=f.get("text"),
                            exchange=Exchange.GATE,
                            environment=ExecutionEnvironment.TESTNET,
                            symbol=symbol,
                            side=OrderSide.BUY if size > 0 else OrderSide.SELL,
                            quantity=float(abs(size) * multiplier),
                            price=float(number(f["price"])),
                            fee=float(number(f["fee"])),
                            fee_asset="USDT",
                            occurred_at=datetime.fromtimestamp(
                                float(number(f["create_time"])), timezone.utc
                            ),
                        )
                    )
            available, total = number(account["available"]), number(account["total"])
            result.balances.append(
                VenueBalanceSnapshot(
                    exchange=Exchange.GATE,
                    environment=ExecutionEnvironment.TESTNET,
                    asset="USDT",
                    available=float(available),
                    locked=float(max(total - available, 0)),
                    equity=float(total + number(account["unrealised_pnl"])),
                )
            )
            return result
        except Exception:
            raise ExternalServiceError(
                "Gate reconciliation incomplete or invalid"
            ) from None

    async def aclose(self):
        # Journal/store/metadata ownership belongs to the runtime, not the adapter.
        await self._transport.close()
