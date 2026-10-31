"""Offline execution-contract tests. No account or real exchange is accessed."""

import asyncio
from copy import deepcopy

import pytest

from app.core.errors import AmbiguousExecutionError, ExternalServiceError, SecurityError
from app.demo.gate_cycle_journal import GateCycleJournal, CycleBlocked
from app.execution.adapters.gate_testnet import GateTestnetExecutionAdapter
from app.execution.adapters.gate_order_constraints import gate_client_id
from app.schemas.common import Exchange, OrderSide
from app.schemas.oms import ExecutionEnvironment, OMSOrder, OMSOrderStatus


def order(**changes):
    values = dict(
        oms_order_id="order-0001",
        client_order_id=gate_client_id("a" * 64),
        decision_id="decision",
        risk_check_id="risk",
        approval_id="a" * 64,
        request_fingerprint="b" * 64,
        correlation_id="corr",
        exchange=Exchange.GATE,
        environment=ExecutionEnvironment.TESTNET,
        symbol="BTCUSDT",
        timeframe="15m",
        strategy="TEST",
        side=OrderSide.BUY,
        quantity=0.0002,
        requested_notional=20,
        reference_price=80000,
        status=OMSOrderStatus.PENDING_SUBMISSION,
    )
    values.update(changes)
    return OMSOrder(**values)


async def metadata(symbol):
    return (
        dict(
            name=symbol.replace("USDT", "_USDT"),
            quanto_multiplier=".0001",
            order_size_min="1",
            order_size_max="100",
            in_delisting=False,
            order_price_round=".1",
            mark_price="80000",
        ),
        1000,
    )


class Protection:
    async def verify(self, _order):
        return (
            dict(verified=True, stop_loss="79000", take_profit="81000")
            if _order.side == OrderSide.BUY
            else dict(verified=True, stop_loss="81000", take_profit="79000")
        )


class Venue:
    def __init__(self, journal):
        self.journal = journal
        self.writes = []
        self.orders = {}
        self.stop = None
        self.size = 0
        self.partial = False
        self.failure = None
        self.foreign = False
        self.leverage = "1"
        self.after_close_hook = lambda row: row
        self.read_entry_hook = lambda row: row

    async def account(self):
        return dict(
            currency="USDT",
            total="1000",
            available="984",
            in_dual_mode=False,
            unrealised_pnl="0",
        )

    async def rows(self, kind, **_kwargs):
        if kind == "positions":
            rows = [await self.get_position("BTC_USDT")]
            if self.foreign:
                rows.append(dict(contract="DOGE_USDT", size=1))
            return rows
        if kind == "orders":
            return []
        if kind == "stops":
            return [deepcopy(self.stop)] if self.stop else []
        if kind == "fills":
            return []
        raise AssertionError(kind)

    async def get_position(self, contract):
        return dict(
            contract=contract,
            size=self.size,
            mode="single",
            leverage=self.leverage,
            entry_price="80000" if self.size else "0",
            mark_price="80000",
            unrealised_pnl="0",
        )

    async def place_entry(self, **kwargs):
        assert self.journal.read("order-0001")["state"] == "ENTRY_PENDING"
        self.writes.append("entry")
        if self.failure == "entry":
            raise TimeoutError("SENSITIVE PROVIDER MESSAGE")
        if self.failure == "cancel":
            raise asyncio.CancelledError()
        self.size = 1 if self.partial else kwargs["signed_contracts"]
        self.orders["11"] = dict(
            id="11",
            contract=kwargs["contract"],
            text=kwargs["client_id"],
            size=kwargs["signed_contracts"],
            left=abs(kwargs["signed_contracts"]) - abs(self.size),
            status="finished",
            finish_as="ioc" if self.partial else "filled",
            is_close=False,
            is_reduce_only=False,
            is_liq=False,
            fill_price="80000",
        )
        return {"id": "11"}

    async def get_order(self, oid):
        data = deepcopy(self.orders[oid])
        return (
            self.read_entry_hook(data) if oid == "11" else self.after_close_hook(data)
        )

    async def place_stop(self, **kwargs):
        assert self.journal.read("order-0001")["state"] == "STOP_SUBMITTING"
        self.writes.append("stop")
        if self.failure == "stop":
            raise TimeoutError("SENSITIVE PROVIDER MESSAGE")
        qty = kwargs["signed_position"]
        self.stop = dict(
            id="22",
            status="open",
            create_time=1000,
            order_type="close-long-position" if qty > 0 else "close-short-position",
            initial=dict(
                contract=kwargs["contract"],
                size=0,
                price="0",
                tif="ioc",
                is_close=True,
                is_reduce_only=True,
            ),
            trigger=dict(
                price=kwargs["stop_price"],
                strategy_type=0,
                price_type=1,
                rule=2 if qty > 0 else 1,
                expiration=3600,
            ),
        )
        return {"id": "22"}

    async def get_stop(self, oid):
        assert oid == "22"
        data = deepcopy(self.stop)
        if self.failure == "stop_read":
            data["trigger"]["price"] = "78000"
        return data

    async def close_position(self, **kwargs):
        assert self.journal.read("order-0001")["state"] in {
            "EXIT_PENDING",
            "EMERGENCY_EXIT_PENDING",
        }
        self.writes.append("close")
        if self.failure == "close":
            raise TimeoutError("SENSITIVE PROVIDER MESSAGE")
        closed_size = self.size
        self.size = 0
        self.orders["33"] = dict(
            id="33",
            contract=kwargs["contract"],
            text=kwargs["client_id"],
            # Observed Gate response normalizes a close-all request to fills.
            size=-closed_size,
            left=0,
            status="finished",
            finish_as="filled",
            is_close=True,
            is_reduce_only=True,
            is_liq=False,
            fill_price="80000",
        )
        return {"id": "33"}

    async def cancel_stop(self, oid):
        assert self.size == 0 and oid == "22"
        self.writes.append("cancel_stop")
        if self.failure == "cancel_stop":
            raise TimeoutError()
        self.stop = None
        return {"id": "22"}

    async def close(self):
        pass


@pytest.fixture
def setup(tmp_path):
    journal = GateCycleJournal(tmp_path / "journal.db")
    venue = Venue(journal)

    async def guard():
        return True

    adapter = GateTestnetExecutionAdapter(
        metadata,
        lambda: 1001,
        transport=venue,
        journal=journal,
        protection_store=Protection(),
        account_guard=guard,
        execution_enabled=True,
    )
    yield adapter, venue, journal
    journal.close()


@pytest.mark.asyncio
async def test_complete_owned_cycle_and_independent_reconciliation(setup):
    adapter, venue, journal = setup
    result = await adapter.submit_order(order())
    assert (
        result.status == OMSOrderStatus.FILLED
        and result.cumulative_filled_quantity == 0.0002
    )
    assert journal.read("order-0001")["state"] == "PROTECTED"
    state = await adapter.fetch_state(symbols={"BTCUSDT"})
    assert state.orders[0].venue_order_id == "11"
    assert state.positions[0].quantity == 0.0002
    assert state.balances[0].equity == 1000
    assert (await adapter.cancel_order(order())).status == OMSOrderStatus.FILLED
    assert venue.writes == ["entry", "stop"]  # cancel entry never cancels protection
    result = await adapter.close_protected_order(order())
    assert result["state"] == "CLOSED"
    assert journal.recovery_status()["recovery_required"] is False
    assert venue.writes == ["entry", "stop", "close", "cancel_stop"]


@pytest.mark.asyncio
async def test_partial_ioc_protects_actual_filled_contracts(setup):
    adapter, venue, journal = setup
    venue.partial = True
    result = await adapter.submit_order(order())
    assert result.status == OMSOrderStatus.CANCELED
    assert result.cumulative_filled_quantity == 0.0001
    assert journal.read("order-0001")["position"] == "1"
    assert journal.read("order-0001")["state"] == "PROTECTED"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["entry", "stop", "stop_read"])
async def test_unknown_write_or_invalid_stop_halts_without_retry(setup, failure):
    adapter, venue, journal = setup
    venue.failure = failure
    with pytest.raises(AmbiguousExecutionError) as error:
        await adapter.submit_order(order())
    assert "SENSITIVE" not in str(error.value)
    assert journal.read("order-0001")["state"] == "HALTED"
    writes = list(venue.writes)
    with pytest.raises((CycleBlocked, SecurityError)):
        await adapter.submit_order(order())
    assert venue.writes == writes


@pytest.mark.asyncio
async def test_task_cancellation_persists_uncertain_dispatch(setup):
    adapter, venue, journal = setup
    venue.failure = "cancel"
    with pytest.raises(asyncio.CancelledError):
        await adapter.submit_order(order())
    assert journal.read("order-0001")["state"] == "HALTED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"client_order_id": "wrong-client"},
        {"quantity": 0.00025},
        {"leverage": 2},
        {"exchange": Exchange.BYBIT},
        {"environment": ExecutionEnvironment.PAPER},
    ],
)
async def test_invalid_approval_scope_never_writes(setup, changes):
    adapter, venue, journal = setup
    with pytest.raises(SecurityError):
        await adapter.submit_order(order(**changes))
    assert not venue.writes and not journal.recovery_status()["recovery_required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "risk", "protection", "foreign", "cross"])
async def test_preflight_controls_block_before_any_write(setup, kind):
    adapter, venue, journal = setup
    if kind == "disabled":
        adapter._enabled = False
    if kind == "risk":

        async def guard():
            return False

        adapter._guard = guard
    if kind == "protection":

        async def verify(_order):
            return {"verified": False}

        adapter._protection.verify = verify
    if kind == "foreign":
        venue.foreign = True
    if kind == "cross":
        venue.leverage = "0"
    with pytest.raises(Exception):
        await adapter.submit_order(order())
    assert venue.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "99"),
        ("text", "t-other"),
        ("contract", "ETH_USDT"),
        ("left", 1),
        ("fill_price", "78000"),
    ],
)
async def test_unconfirmed_or_unprotected_entry_never_reports_success(
    setup, field, value
):
    adapter, venue, journal = setup

    def alter(data):
        data[field] = value
        return data

    venue.read_entry_hook = alter
    with pytest.raises(AmbiguousExecutionError):
        await adapter.submit_order(order())
    assert journal.read("order-0001")["state"] == "HALTED"
    assert venue.writes == (["entry", "close"] if field == "fill_price" else ["entry"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "99"),
        ("text", "t-other"),
        ("contract", "ETH_USDT"),
        ("is_reduce_only", False),
        ("is_liq", True),
        ("left", 1),
    ],
)
async def test_close_scope_verified_before_removing_stop(setup, field, value):
    adapter, venue, journal = setup
    await adapter.submit_order(order())

    def alter(data):
        data[field] = value
        return data

    venue.after_close_hook = alter
    with pytest.raises(AmbiguousExecutionError):
        await adapter.close_protected_order(order())
    assert journal.read("order-0001")["state"] == "HALTED"
    assert "cancel_stop" not in venue.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["close", "cancel_stop"])
async def test_unknown_exit_never_retries_or_releases_slot(setup, failure):
    adapter, venue, journal = setup
    await adapter.submit_order(order())
    venue.failure = failure
    with pytest.raises(AmbiguousExecutionError):
        await adapter.close_protected_order(order())
    before = list(venue.writes)
    with pytest.raises(SecurityError):
        await adapter.close_protected_order(order())
    assert venue.writes == before and journal.read("order-0001")["state"] == "HALTED"


@pytest.mark.asyncio
async def test_restart_preserves_halt_and_blocks_second_dispatch(tmp_path):
    path = tmp_path / "journal.db"
    journal = GateCycleJournal(path)
    venue = Venue(journal)

    async def guard():
        return True

    def adapter(j):
        return GateTestnetExecutionAdapter(
            metadata,
            lambda: 1001,
            transport=venue,
            journal=j,
            protection_store=Protection(),
            account_guard=guard,
            execution_enabled=True,
        )

    venue.failure = "entry"
    with pytest.raises(AmbiguousExecutionError):
        await adapter(journal).submit_order(order())
    journal.close()
    reopened = GateCycleJournal(path)
    try:
        with pytest.raises(CycleBlocked):
            await adapter(reopened).submit_order(order())
        assert venue.writes == ["entry"]
        assert reopened.read("order-0001")["state"] == "HALTED"
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_missing_protection_is_not_a_healthy_snapshot(setup):
    adapter, venue, journal = setup
    await adapter.submit_order(order())
    venue.stop = None
    with pytest.raises(ExternalServiceError):
        await adapter.fetch_state(symbols={"BTCUSDT"})
    assert journal.read("order-0001")["state"] == "HALTED"


@pytest.mark.asyncio
async def test_empty_oms_scope_still_reads_account_and_detects_foreign_exposure(setup):
    adapter, venue, _ = setup
    assert (await adapter.fetch_state(symbols=set())).balances
    venue.foreign = True
    with pytest.raises(ExternalServiceError):
        await adapter.fetch_state(symbols=set())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stop", "stop_read"])
async def test_stop_failure_attempts_one_owned_reduce_only_close_and_keeps_halt(
    setup, failure
):
    adapter, venue, journal = setup
    venue.failure = failure
    with pytest.raises(AmbiguousExecutionError):
        await adapter.submit_order(order())
    assert venue.writes == ["entry", "stop", "close"] and venue.size == 0
    assert journal.read("order-0001")["state"] == "HALTED"
    assert (
        journal.db.execute("SELECT reason FROM events ORDER BY seq DESC LIMIT 1")
        .fetchone()[0]
        .startswith("emergency close verified")
    )
    assert journal.recovery_status()["recovery_required"] is True
    # No blind cancellation of an uncertain stop, and no automatic reset.
    with pytest.raises((CycleBlocked, SecurityError)):
        await adapter.submit_order(order())
    assert venue.writes == ["entry", "stop", "close"]


@pytest.mark.asyncio
async def test_emergency_close_uncertainty_is_never_retried(setup):
    adapter, venue, journal = setup
    venue.failure = "stop"

    async def fail_close(**kwargs):
        assert journal.read("order-0001")["state"] == "EMERGENCY_EXIT_PENDING"
        venue.writes.append("close")
        raise TimeoutError()

    venue.close_position = fail_close
    with pytest.raises(AmbiguousExecutionError):
        await adapter.submit_order(order())
    assert journal.read("order-0001")["state"] == "HALTED" and venue.size == 2
    assert venue.writes == ["entry", "stop", "close"]


@pytest.mark.asyncio
async def test_changed_position_prevents_emergency_close(setup):
    adapter, venue, journal = setup
    original = venue.place_stop

    async def fail_and_change(**kwargs):
        try:
            return await original(**kwargs)
        finally:
            venue.size = 9

    venue.place_stop = fail_and_change
    venue.failure = "stop"
    with pytest.raises(AmbiguousExecutionError):
        await adapter.submit_order(order())
    assert venue.writes == ["entry", "stop"]
    assert journal.read("order-0001")["state"] == "HALTED"


@pytest.mark.asyncio
async def test_default_adapter_is_disabled_and_healthcheck_fails_closed(setup):
    _, venue, journal = setup

    async def guard():
        return True

    adapter = GateTestnetExecutionAdapter(
        metadata,
        lambda: 1001,
        transport=venue,
        journal=journal,
        protection_store=Protection(),
        account_guard=guard,
    )
    assert not await adapter.healthcheck()
    with pytest.raises(SecurityError):
        await adapter.submit_order(order())
    assert not venue.writes


@pytest.mark.asyncio
async def test_short_cycle_has_opposite_stop_rule_and_can_close(setup):
    adapter, venue, journal = setup
    short = order(side=OrderSide.SELL)
    snapshot = await adapter.submit_order(short)
    assert snapshot.side == OrderSide.SELL and venue.size == -2
    assert (
        venue.stop["order_type"] == "close-short-position"
        and venue.stop["trigger"]["rule"] == 1
    )
    assert venue.stop["trigger"]["price"] == "81000"
    assert (
        await adapter.healthcheck()
    )  # protected exposure is healthy, pending exposure is not
    await adapter.close_protected_order(short)
    assert journal.read("order-0001")["state"] == "CLOSED"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "close"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("exchange", Exchange.BYBIT),
        ("environment", ExecutionEnvironment.PAPER),
        ("client_order_id", "t-not-owned"),
    ],
)
async def test_lifecycle_commands_cannot_cross_scope_or_ownership(
    setup, operation, field, value
):
    adapter, venue, journal = setup
    await adapter.submit_order(order())
    method = (
        adapter.cancel_order if operation == "cancel" else adapter.close_protected_order
    )
    with pytest.raises(SecurityError):
        await method(order(**{field: value}))
    assert (
        venue.writes == ["entry", "stop"]
        and journal.read("order-0001")["state"] == "PROTECTED"
    )


@pytest.mark.asyncio
async def test_current_price_above_approval_bound_is_rejected_before_dispatch(setup):
    adapter, venue, _ = setup

    async def expensive(symbol):
        data, observed = await metadata(symbol)
        data["mark_price"] = "82000"
        return data, observed

    adapter._metadata_provider = expensive
    with pytest.raises(SecurityError):
        await adapter.submit_order(order())
    assert not venue.writes


@pytest.mark.asyncio
async def test_real_transport_full_cycle_with_mock_http_and_exact_signed_requests(
    setup,
):
    import hashlib
    import hmac
    import json
    import httpx
    from app.demo.gate_testnet_transport import (
        GateTestnetTransport,
        TestnetCredentials as Credentials,
    )

    adapter, venue, journal = setup
    requests = []

    async def handler(request):
        requests.append(request)
        assert request.url.host == "api-testnet.gateapi.io"
        signature = f"{request.method}\n{request.url.path}\n{request.url.query.decode()}\n{hashlib.sha512(request.content).hexdigest()}\n1001"
        assert (
            request.headers["SIGN"]
            == hmac.new(
                b"offline-secret", signature.encode(), hashlib.sha512
            ).hexdigest()
        )
        path = request.url.path.removeprefix("/api/v4/futures/usdt/")
        body = json.loads(request.content) if request.content else {}
        if request.method == "POST" and path == "orders":
            if body.get("close"):
                assert (
                    body["size"] == 0
                    and body["reduce_only"] is True
                    and body["tif"] == "ioc"
                )
                result = await venue.close_position(
                    contract=body["contract"], client_id=body["text"]
                )
            else:
                assert body["price"] == "0" and body["tif"] == "ioc"
                result = await venue.place_entry(
                    contract=body["contract"],
                    signed_contracts=body["size"],
                    client_id=body["text"],
                )
        elif request.method == "POST" and path == "price_orders":
            assert (
                body["initial"]["size"] == 0 and body["initial"]["reduce_only"] is True
            )
            result = await venue.place_stop(
                contract=body["initial"]["contract"],
                signed_position=venue.size,
                stop_price=body["trigger"]["price"],
            )
        elif request.method == "DELETE":
            result = await venue.cancel_stop(path.split("/")[-1])
        elif path.startswith("positions/"):
            result = await venue.get_position(path.split("/")[-1])
        elif path.startswith("orders/"):
            result = await venue.get_order(path.split("/")[-1])
        elif path.startswith("price_orders/"):
            result = await venue.get_stop(path.split("/")[-1])
        elif path == "accounts":
            result = await venue.account()
        else:
            result = await venue.rows(
                {
                    "positions": "positions",
                    "orders": "orders",
                    "price_orders": "stops",
                    "my_trades": "fills",
                }[path]
            )
        return httpx.Response(200, json=result)

    transport = GateTestnetTransport(
        Credentials("offline-key", "offline-secret"),
        trusted_clock=lambda: 1001,
        writes_enabled=True,
        acknowledgement="GATE_TESTNET_VIRTUAL_FUNDS_ONLY",
        transport=httpx.MockTransport(handler),
    )
    adapter._transport = transport
    try:
        await adapter.submit_order(order())
        await adapter.fetch_state(symbols={"BTCUSDT"})
        await adapter.close_protected_order(order())
        assert journal.read("order-0001")["state"] == "CLOSED"
        writes = [r for r in requests if r.method in {"POST", "DELETE"}]
        assert [r.method for r in writes] == ["POST", "POST", "POST", "DELETE"]
        assert len([r for r in requests if r.url.path.endswith("/orders/11")]) >= 2
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_known_zero_fill_cancels_cleanly_and_releases_slot_without_stop(setup):
    adapter, venue, journal = setup
    original = venue.place_entry

    async def zero_fill(**kwargs):
        ack = await original(**kwargs)
        venue.size = 0
        venue.orders["11"].update(
            left=kwargs["signed_contracts"], finish_as="ioc", fill_price="0"
        )
        return ack

    venue.place_entry = zero_fill
    result = await adapter.submit_order(order())
    assert (
        result.status == OMSOrderStatus.CANCELED
        and result.cumulative_filled_quantity == 0
    )
    assert venue.writes == ["entry"] and journal.read("order-0001")["state"] == "CLOSED"
    assert not journal.recovery_status()["recovery_required"]


@pytest.mark.asyncio
async def test_zero_fill_with_orphan_exposure_still_halts(setup):
    adapter, venue, journal = setup
    original = venue.place_entry

    async def zero_fill(**kwargs):
        ack = await original(**kwargs)
        venue.size = 0
        venue.foreign = True
        venue.orders["11"].update(
            left=kwargs["signed_contracts"], finish_as="ioc", fill_price="0"
        )
        return ack

    venue.place_entry = zero_fill
    with pytest.raises(AmbiguousExecutionError):
        await adapter.submit_order(order())
    assert journal.read("order-0001")["state"] == "HALTED" and venue.writes == ["entry"]


@pytest.mark.asyncio
async def test_risk_revoked_after_intent_does_not_dispatch(setup):
    adapter, venue, journal = setup
    checks = 0

    async def guard():
        nonlocal checks
        checks += 1
        return checks == 1

    adapter._guard = guard
    with pytest.raises(AmbiguousExecutionError):
        await adapter.submit_order(order())
    assert not venue.writes and journal.read("order-0001")["state"] == "HALTED"


@pytest.mark.asyncio
async def test_reconciliation_serializes_with_active_dispatch(setup):
    adapter, venue, journal = setup
    started, resume = asyncio.Event(), asyncio.Event()
    original = venue.place_entry

    async def hold(**kwargs):
        started.set()
        await resume.wait()
        return await original(**kwargs)

    venue.place_entry = hold
    submit = asyncio.create_task(adapter.submit_order(order()))
    await started.wait()
    read = asyncio.create_task(adapter.fetch_state(symbols={"BTCUSDT"}))
    await asyncio.sleep(0)
    assert not read.done()
    resume.set()
    await submit
    state = await read
    assert state.orders[0].status == OMSOrderStatus.FILLED
    assert journal.read("order-0001")["state"] == "PROTECTED"


@pytest.mark.asyncio
async def test_balance_equity_includes_unrealized_pnl_and_fills_use_contract_multiplier(
    setup,
):
    adapter, venue, _ = setup
    original = venue.rows

    async def rows(kind, **kwargs):
        if kind == "fills":
            return (
                [
                    dict(
                        id="42",
                        order_id="11",
                        text="t-owned",
                        contract="BTC_USDT",
                        size="-2",
                        price="81000",
                        fee=".01",
                        create_time="1000.5",
                    )
                ]
                if kwargs["contract"] == "BTC_USDT"
                else []
            )
        return await original(kind, **kwargs)

    async def account():
        return dict(currency="USDT", total="1000", available="984", unrealised_pnl="-2")

    venue.rows, venue.account = rows, account
    state = await adapter.fetch_state(symbols={"BTCUSDT"})
    assert state.balances[0].equity == 998
    assert state.fills[0].side == OrderSide.SELL and state.fills[0].quantity == 0.0002
    assert state.fills[0].fee == 0.01


@pytest.mark.asyncio
async def test_closed_entry_history_retention_does_not_break_reconciliation(setup):
    adapter, venue, journal = setup
    result = await adapter.submit_order(order())
    await adapter.close_protected_order(order())
    venue.orders.clear()  # exchange no longer serves historical order details
    state = await adapter.fetch_state(symbols={"BTCUSDT"})
    assert state.orders[0] == result
    assert state.orders[0].observed_at == result.observed_at
    assert journal.read("order-0001")["state"] == "CLOSED"
