"""Month 7 completion tests: OMS, TESTNET adapters and reconciliation."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from app.audit.service import AuditService
from app.api.context import build_context
from app.core.config import Settings
from app.core.errors import (
    AmbiguousExecutionError,
    ExternalServiceError,
    RiskError,
    SecurityError,
)
from app.core.state_machine import SystemState, SystemStateMachine
from app.database.models import (
    ExecutionCommandModel,
    OMSOrderEventModel,
    OMSOrderModel,
    OrderApprovalModel,
)
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.execution.adapters.base import ExchangeExecutionAdapter
from app.execution.adapters.binance_testnet import (
    BinanceTestnetExecutionAdapter,
)
from app.execution.adapters.bybit_testnet import (
    BybitTestnetExecutionAdapter,
)
from app.execution.adapters.paper import PaperExecutionAdapter
from app.execution.credentials import TestnetCredentials as SandboxCredentials
from app.oms.reconciliation import (
    ReconciliationService,
    _compare_orders,
    _compare_positions,
)
from app.oms.service import OMSService
from app.paper_trading.engine import PaperTradingEngine
from app.risk.manager import RiskManager
from app.schemas.common import CandidateAction, Exchange, OrderSide
from app.schemas.oms import (
    ExecutionCommand,
    ExecutionEnvironment,
    ExecutionFill,
    OMSOrder,
    OMSOrderStatus,
    OMSOrderType,
    ReconciliationMismatch,
    ReconciliationMismatchType,
    ReconciliationRun,
    ReconciliationRunStatus,
    ReconciliationSeverity,
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueStateSnapshot,
)
from app.schemas.risk import ApprovalStatus, RiskLimits
from app.tests.conftest import make_decision

CONTRACT_ROOT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "contracts"
    / "schemas"
    / "v1"
)


def sample_oms_order(*, exchange: Exchange = Exchange.BINANCE) -> OMSOrder:
    return OMSOrder(
        client_order_id="cc-" + ("a" * 32),
        decision_id="decision",
        risk_check_id="risk-check",
        approval_id="a" * 64,
        request_fingerprint="b" * 64,
        correlation_id="correlation",
        exchange=exchange,
        environment=ExecutionEnvironment.TESTNET,
        symbol="BTCUSDT",
        timeframe="15m",
        strategy="TEST",
        side=OrderSide.BUY,
        order_type=OMSOrderType.MARKET,
        quantity=0.01,
        requested_notional=1_000,
        reference_price=100_000,
        status=OMSOrderStatus.PENDING_SUBMISSION,
    )


async def operational_state_machine() -> SystemStateMachine:
    state_machine = SystemStateMachine()
    await state_machine.transition(
        SystemState.INITIALIZING,
        reason="test",
        actor="test",
    )
    await state_machine.transition(
        SystemState.PAPER,
        reason="test",
        actor="test",
    )
    return state_machine


class FakeTestnetAdapter(ExchangeExecutionAdapter):
    exchange = Exchange.BINANCE
    environment = ExecutionEnvironment.TESTNET

    def __init__(self) -> None:
        self.submit_calls = 0
        self.cancel_calls = 0
        self.snapshot = VenueStateSnapshot(
            exchange=self.exchange,
            environment=self.environment,
        )

    async def healthcheck(self) -> bool:
        return True

    async def submit_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        self.submit_calls += 1
        return VenueOrderSnapshot(
            exchange=self.exchange,
            environment=self.environment,
            venue_order_id="venue-1",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            status=OMSOrderStatus.SUBMITTED,
            quantity=order.quantity,
        )

    async def cancel_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        self.cancel_calls += 1
        return VenueOrderSnapshot(
            exchange=self.exchange,
            environment=self.environment,
            venue_order_id=order.venue_order_id or "venue-1",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            status=OMSOrderStatus.CANCEL_PENDING,
            quantity=order.quantity,
            cumulative_filled_quantity=order.cumulative_filled_quantity,
        )

    async def fetch_state(
        self,
        *,
        symbols: set[str] | None = None,
    ) -> VenueStateSnapshot:
        return self.snapshot

    async def aclose(self) -> None:
        return None


class AmbiguousAdapter(FakeTestnetAdapter):
    async def submit_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        self.submit_calls += 1
        raise AmbiguousExecutionError("unknown")


async def oms_stack(tmp_path, *, adapter=None):
    database = Database(
        f"sqlite+aiosqlite:///{tmp_path / 'month-7.db'}"
    )
    await database.create_all()
    repository = Repository(database)
    state_machine = await operational_state_machine()
    audit = AuditService(repository=repository)
    risk = RiskManager(
        RiskLimits(),
        state_machine,
        audit,
        repository=repository,
    )
    await risk.initialize()
    paper = PaperTradingEngine(audit, risk, repository=repository)
    adapter = adapter or FakeTestnetAdapter()
    service = OMSService(
        target_environment=ExecutionEnvironment.TESTNET,
        target_exchange=Exchange.BINANCE,
        paper_engine=paper,
        risk_manager=risk,
        audit_service=audit,
        adapters={(Exchange.BINANCE, ExecutionEnvironment.TESTNET): adapter},
        repository=repository,
    )
    decision = make_decision(CandidateAction.BUY)
    check = await risk.check(decision, entry_price=100, atr=1)
    return (
        database,
        repository,
        state_machine,
        audit,
        risk,
        service,
        adapter,
        decision,
        check,
    )


def test_testnet_configuration_is_explicit_and_live_urls_are_rejected():
    with pytest.raises(Exception, match="OMS_TESTNET_ENABLED"):
        Settings(OMS_EXECUTION_ENVIRONMENT="TESTNET")
    with pytest.raises(Exception, match="ACKNOWLEDGEMENT"):
        Settings(
            OMS_EXECUTION_ENVIRONMENT="TESTNET",
            OMS_TESTNET_ENABLED=True,
        )
    configured = Settings(
        OMS_EXECUTION_ENVIRONMENT="TESTNET",
        OMS_TESTNET_ENABLED=True,
        OMS_TESTNET_ACKNOWLEDGEMENT="TESTNET_ONLY_NO_REAL_FUNDS",
    )
    assert configured.oms_execution_environment == "TESTNET"
    with pytest.raises(Exception, match="requires PostgreSQL"):
        build_context(configured, with_database=True)
    with pytest.raises(Exception, match="exact Spot TESTNET"):
        Settings(BINANCE_TESTNET_REST_URL="https://api.binance.com")
    with pytest.raises(Exception, match="only Bybit linear"):
        Settings(BYBIT_TESTNET_CATEGORY="spot")
    with pytest.raises(Exception, match="exact TESTNET"):
        Settings(BYBIT_TESTNET_REST_URL="https://api.bybit.com")


def test_credentials_are_redacted_and_adapters_reject_live_hosts():
    credentials = SandboxCredentials("test-key-id", "test-signing-secret")
    assert "test-key-id" not in repr(credentials)
    assert "test-signing-secret" not in repr(credentials)
    with pytest.raises(SecurityError):
        BinanceTestnetExecutionAdapter(
            credentials,
            base_url="https://api.binance.com",
        )
    with pytest.raises(SecurityError):
        BybitTestnetExecutionAdapter(
            credentials,
            base_url="https://api.bybit.com",
        )
    with pytest.raises(PydanticValidationError):
        ExecutionCommand(
            oms_order_id="order",
            command_type="SUBMIT",
            max_attempts=2,
        )


async def test_binance_signature_and_ambiguous_write_are_fail_closed():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "orderId": 42,
                "clientOrderId": "cc-" + ("a" * 32),
                "side": "BUY",
                "type": "MARKET",
                "status": "NEW",
                "origQty": "0.01",
                "executedQty": "0",
                "cummulativeQuoteQty": "0",
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://testnet.binance.vision",
    )
    credentials = SandboxCredentials("test-key-id", "test-signing-secret")
    adapter = BinanceTestnetExecutionAdapter(
        credentials,
        client=client,
        clock_ms=lambda: 123456789,
    )
    snapshot = await adapter.submit_order(sample_oms_order())
    assert snapshot.status == OMSOrderStatus.SUBMITTED
    request = captured["request"]
    pairs = parse_qsl(request.url.query.decode())
    signature = dict(pairs).pop("signature")
    unsigned = "&".join(
        f"{key}={value}" for key, value in pairs if key != "signature"
    )
    expected = hmac.new(
        b"test-signing-secret",
        unsigned.encode(),
        hashlib.sha256,
    ).hexdigest()
    assert signature == expected
    assert request.headers["X-MBX-APIKEY"] == "test-key-id"

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("unknown", request=request)

    timeout_client = httpx.AsyncClient(
        transport=httpx.MockTransport(timeout_handler),
        base_url="https://testnet.binance.vision",
    )
    timeout_adapter = BinanceTestnetExecutionAdapter(
        credentials,
        client=timeout_client,
    )
    with pytest.raises(AmbiguousExecutionError):
        await timeout_adapter.submit_order(sample_oms_order())
    await client.aclose()
    await timeout_client.aclose()


async def test_testnet_adapters_normalize_quantity_before_persistence():
    credentials = SandboxCredentials(
        "test-key-id",
        "test-signing-secret",
    )

    async def binance_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/exchangeInfo"
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {
                                "filterType": "MARKET_LOT_SIZE",
                                "minQty": "0.001",
                                "maxQty": "10",
                                "stepSize": "0.001",
                            },
                            {
                                "filterType": "MIN_NOTIONAL",
                                "minNotional": "10",
                                "applyToMarket": True,
                            },
                        ],
                    }
                ]
            },
        )

    binance_client = httpx.AsyncClient(
        transport=httpx.MockTransport(binance_handler),
        base_url="https://testnet.binance.vision",
    )
    binance = BinanceTestnetExecutionAdapter(
        credentials,
        client=binance_client,
    )
    requested = sample_oms_order().model_copy(
        update={
            "quantity": 0.012345,
            "requested_notional": 1_234.5,
        }
    )
    normalized = await binance.prepare_order(requested)
    assert normalized.quantity == 0.012
    assert normalized.requested_notional == 1_200
    await binance_client.aclose()

    async def bybit_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v5/market/instruments-info"
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "lotSizeFilter": {
                                "minOrderQty": "0.01",
                                "maxMktOrderQty": "10",
                                "qtyStep": "0.01",
                                "minNotionalValue": "10",
                            },
                        }
                    ]
                },
            },
        )

    bybit_client = httpx.AsyncClient(
        transport=httpx.MockTransport(bybit_handler),
        base_url="https://api-testnet.bybit.com",
    )
    bybit = BybitTestnetExecutionAdapter(
        credentials,
        client=bybit_client,
    )
    normalized = await bybit.prepare_order(
        requested.model_copy(update={"exchange": Exchange.BYBIT})
    )
    assert normalized.quantity == 0.01
    assert normalized.requested_notional == 1_000
    await bybit_client.aclose()


async def test_bybit_v5_signature_uses_exact_sorted_payload():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "orderId": "bybit-order",
                    "orderLinkId": "cc-" + ("a" * 32),
                },
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api-testnet.bybit.com",
    )
    credentials = SandboxCredentials("test-key-id", "test-signing-secret")
    adapter = BybitTestnetExecutionAdapter(
        credentials,
        client=client,
        clock_ms=lambda: 123456789,
    )
    snapshot = await adapter.submit_order(
        sample_oms_order(exchange=Exchange.BYBIT)
    )
    assert snapshot.status == OMSOrderStatus.SUBMITTED
    request = captured["request"]
    body = request.content.decode()
    expected = hmac.new(
        b"test-signing-secret",
        ("123456789" + "test-key-id" + "5000" + body).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["X-BAPI-SIGN"] == expected
    assert json.loads(body)["orderLinkId"] == "cc-" + ("a" * 32)
    await client.aclose()


async def test_adapters_fetch_all_open_orders_for_orphan_detection():
    credentials = SandboxCredentials(
        "test-key-id",
        "test-signing-secret",
    )

    async def binance_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/account":
            return httpx.Response(200, json={"balances": []})
        if request.url.path in {
            "/api/v3/openOrders",
            "/api/v3/allOrders",
        }:
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "ETHUSDT",
                        "orderId": 99,
                        "clientOrderId": "manual-orphan-order",
                        "side": "BUY",
                        "type": "LIMIT",
                        "status": "NEW",
                        "origQty": "1",
                        "executedQty": "0",
                        "cummulativeQuoteQty": "0",
                    }
                ],
            )
        if request.url.path == "/api/v3/myTrades":
            return httpx.Response(200, json=[])
        raise AssertionError(request.url.path)

    binance_client = httpx.AsyncClient(
        transport=httpx.MockTransport(binance_handler),
        base_url="https://testnet.binance.vision",
    )
    binance = BinanceTestnetExecutionAdapter(
        credentials,
        client=binance_client,
    )
    binance_state = await binance.fetch_state(symbols=set())
    assert binance_state.orders[0].client_order_id == "manual-orphan-order"
    await binance_client.aclose()

    bybit_open_modes = []

    async def bybit_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v5/order/realtime":
            open_only = dict(parse_qsl(request.url.query.decode()))[
                "openOnly"
            ]
            bybit_open_modes.append(open_only)
            rows = (
                [
                    {
                        "orderId": "manual-bybit",
                        "orderLinkId": "manual-bybit-order",
                        "symbol": "ETHUSDT",
                        "side": "Buy",
                        "orderType": "Limit",
                        "orderStatus": "New",
                        "qty": "1",
                        "cumExecQty": "0",
                        "avgPrice": "",
                    }
                ]
                if open_only == "0"
                else []
            )
            return httpx.Response(
                200,
                json={"retCode": 0, "result": {"list": rows}},
            )
        if request.url.path == "/v5/order/history":
            return httpx.Response(
                200,
                json={"retCode": 0, "result": {"list": []}},
            )
        if request.url.path in {
            "/v5/execution/list",
            "/v5/position/list",
        }:
            return httpx.Response(
                200,
                json={"retCode": 0, "result": {"list": []}},
            )
        if request.url.path == "/v5/account/wallet-balance":
            return httpx.Response(
                200,
                json={"retCode": 0, "result": {"list": []}},
            )
        raise AssertionError(request.url.path)

    bybit_client = httpx.AsyncClient(
        transport=httpx.MockTransport(bybit_handler),
        base_url="https://api-testnet.bybit.com",
    )
    bybit = BybitTestnetExecutionAdapter(
        credentials,
        client=bybit_client,
    )
    bybit_state = await bybit.fetch_state(symbols={"BTCUSDT"})
    assert sorted(bybit_open_modes) == ["0", "1"]
    assert bybit_state.orders[0].client_order_id == "manual-bybit-order"
    await bybit_client.aclose()


async def test_bybit_hedge_mode_fails_closed():
    async def handler(request: httpx.Request) -> httpx.Response:
        rows = []
        if request.url.path == "/v5/position/list":
            rows = [
                {
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "size": "0.01",
                    "positionIdx": 1,
                    "avgPrice": "100000",
                    "markPrice": "100000",
                    "unrealisedPnl": "0",
                }
            ]
        return httpx.Response(
            200,
            json={"retCode": 0, "result": {"list": rows}},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api-testnet.bybit.com",
    )
    adapter = BybitTestnetExecutionAdapter(
        SandboxCredentials("test-key-id", "test-signing-secret"),
        client=client,
    )
    with pytest.raises(ExternalServiceError, match="hedge mode"):
        await adapter.fetch_state()
    await client.aclose()


async def test_testnet_order_is_persisted_before_single_submission(tmp_path):
    (
        database,
        repository,
        _,
        _,
        _,
        service,
        adapter,
        decision,
        check,
    ) = await oms_stack(tmp_path)
    order = await service.submit_approved(
        decision,
        check,
        current_price=100,
    )
    assert order.status == OMSOrderStatus.PENDING_SUBMISSION
    assert adapter.submit_calls == 0
    async with database.session() as session:
        approval = await session.get(OrderApprovalModel, check.approval_id)
        command = await session.scalar(select(ExecutionCommandModel))
        assert approval.status == ApprovalStatus.CONSUMED.value
        assert approval.paper_order_id is None
        assert approval.oms_order_id == order.oms_order_id
        assert command.status == "PENDING"

    assert await service.dispatch_once()
    assert adapter.submit_calls == 1
    assert not await service.dispatch_once()
    stored = await repository.load_oms_order(order.oms_order_id)
    assert stored.status == OMSOrderStatus.SUBMITTED
    async with database.session() as session:
        events = list(await session.scalars(select(OMSOrderEventModel)))
        assert [event.state_version for event in events] == [1, 2]
    await database.dispose()


async def test_pending_testnet_order_reserves_portfolio_capacity(tmp_path):
    (
        database,
        _,
        _,
        _,
        risk,
        service,
        _,
        decision,
        check,
    ) = await oms_stack(tmp_path)
    second_decision = make_decision(CandidateAction.SELL)
    second_check = await risk.check(
        second_decision,
        entry_price=100,
        atr=1,
    )
    await service.submit_approved(
        decision,
        check,
        current_price=100,
    )
    assert risk.state.open_positions == 1
    with pytest.raises(RiskError, match="stale"):
        await service.submit_approved(
            second_decision,
            second_check,
            current_price=100,
        )
    await database.dispose()


async def test_kill_switch_quarantines_queued_submit_without_venue_call(
    tmp_path,
):
    (
        database,
        repository,
        _,
        _,
        risk,
        service,
        adapter,
        decision,
        check,
    ) = await oms_stack(tmp_path)
    queued = await service.submit_approved(
        decision,
        check,
        current_price=100,
    )
    await risk.trigger_kill_switch(
        reason="operator safety stop",
        actor="test",
    )
    assert await service.dispatch_once()
    assert adapter.submit_calls == 0
    stored = await repository.load_oms_order(queued.oms_order_id)
    assert stored.status == OMSOrderStatus.QUARANTINED
    assert stored.rejection_reason == "KILL_SWITCH_ACTIVE"
    assert risk.state.open_positions == 0
    await database.dispose()


async def test_paper_order_and_oms_mirror_are_atomic_and_idempotent(tmp_path):
    database = Database(
        f"sqlite+aiosqlite:///{tmp_path / 'paper-oms.db'}"
    )
    await database.create_all()
    repository = Repository(database)
    state_machine = await operational_state_machine()
    audit = AuditService(repository=repository)
    risk = RiskManager(
        RiskLimits(),
        state_machine,
        audit,
        repository=repository,
    )
    await risk.initialize()
    paper = PaperTradingEngine(audit, risk, repository=repository)
    adapter = PaperExecutionAdapter(paper)
    service = OMSService(
        target_environment=ExecutionEnvironment.PAPER,
        target_exchange=Exchange.BINANCE,
        paper_engine=paper,
        risk_manager=risk,
        audit_service=audit,
        adapters={(Exchange.BINANCE, ExecutionEnvironment.PAPER): adapter},
        repository=repository,
    )
    decision = make_decision()
    check = await risk.check(decision, entry_price=100, atr=1)
    order = await service.submit_approved(
        decision,
        check,
        current_price=100,
    )
    repeated = await service.submit_approved(
        decision,
        check,
        current_price=100,
    )
    assert repeated == order
    assert order.status == OMSOrderStatus.FILLED
    async with database.session() as session:
        approval = await session.get(OrderApprovalModel, check.approval_id)
        stored = await session.get(OMSOrderModel, order.oms_order_id)
        commands = list(await session.scalars(select(ExecutionCommandModel)))
        assert approval.paper_order_id == order.oms_order_id
        assert approval.oms_order_id is None
        assert stored.status == OMSOrderStatus.FILLED.value
        assert commands == []
    await database.dispose()


async def test_ambiguous_write_is_not_retried_and_waits_for_reconciliation(
    tmp_path,
):
    stack = await oms_stack(tmp_path, adapter=AmbiguousAdapter())
    database, repository, *_, service, adapter, decision, check = stack
    order = await service.submit_approved(
        decision,
        check,
        current_price=100,
    )
    assert await service.dispatch_once()
    assert adapter.submit_calls == 1
    assert not await service.dispatch_once()
    stored = await repository.load_oms_order(order.oms_order_id)
    assert stored.status == OMSOrderStatus.UNKNOWN
    async with database.session() as session:
        command = await session.scalar(select(ExecutionCommandModel))
        assert command.status == "COMPLETED"
        assert command.attempt_count == 1
    await database.dispose()


async def test_expired_max_attempt_lease_becomes_unknown_without_retry(
    tmp_path,
):
    (
        database,
        repository,
        _,
        _,
        _,
        service,
        adapter,
        decision,
        check,
    ) = await oms_stack(tmp_path)
    order = await service.submit_approved(
        decision,
        check,
        current_price=100,
    )
    claimed = await repository.claim_execution_command(
        worker_id="crashed-worker",
        lease_seconds=10,
    )
    assert claimed is not None
    async with database.session() as session, session.begin():
        command = await session.get(
            ExecutionCommandModel,
            claimed[0].command_id,
        )
        command.lease_expires_at = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )

    recovered = await repository.claim_execution_command(
        worker_id="recovery-worker",
        lease_seconds=10,
    )
    assert recovered is None
    assert adapter.submit_calls == 0
    stored = await repository.load_oms_order(order.oms_order_id)
    assert stored.status == OMSOrderStatus.UNKNOWN
    async with database.session() as session:
        command = await session.get(
            ExecutionCommandModel,
            claimed[0].command_id,
        )
        assert command.status == "DEAD_LETTER"
        assert command.last_error_type == "LEASE_EXPIRED"
    await database.dispose()


async def test_cancel_is_queued_before_testnet_adapter_call(tmp_path):
    (
        database,
        repository,
        _,
        _,
        _,
        service,
        adapter,
        decision,
        check,
    ) = await oms_stack(tmp_path)
    order = await service.submit_approved(
        decision,
        check,
        current_price=100,
    )
    await service.dispatch_once()
    queued_cancel = await service.queue_cancel(order.oms_order_id)
    assert queued_cancel.status == OMSOrderStatus.CANCEL_PENDING
    assert adapter.cancel_calls == 0
    repeated = await service.queue_cancel(order.oms_order_id)
    assert repeated.oms_order_id == queued_cancel.oms_order_id
    assert repeated.state_version == queued_cancel.state_version
    async with database.session() as session:
        cancel_commands = list(
            await session.scalars(
                select(ExecutionCommandModel).where(
                    ExecutionCommandModel.command_type == "CANCEL"
                )
            )
        )
        assert len(cancel_commands) == 1
        cancel_command = cancel_commands[0]
        assert cancel_command.status == "PENDING"
    assert await service.dispatch_once()
    assert adapter.cancel_calls == 1
    stored = await repository.load_oms_order(order.oms_order_id)
    assert stored.status == OMSOrderStatus.CANCEL_PENDING
    await database.dispose()


async def test_reconciliation_corrects_status_and_persists_fill(tmp_path):
    (
        database,
        repository,
        _,
        audit,
        risk,
        service,
        adapter,
        decision,
        check,
    ) = await oms_stack(tmp_path)
    queued = await service.submit_approved(
        decision,
        check,
        current_price=100,
    )
    await service.dispatch_once()
    submitted = await repository.load_oms_order(queued.oms_order_id)
    observed_at = datetime.now(timezone.utc)
    adapter.snapshot = VenueStateSnapshot(
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
        orders=[
            VenueOrderSnapshot(
                exchange=Exchange.BINANCE,
                environment=ExecutionEnvironment.TESTNET,
                venue_order_id="venue-1",
                client_order_id=submitted.client_order_id,
                symbol=submitted.symbol,
                side=submitted.side,
                order_type=submitted.order_type,
                status=OMSOrderStatus.FILLED,
                quantity=submitted.quantity,
                cumulative_filled_quantity=submitted.quantity,
                average_fill_price=100,
                observed_at=observed_at,
            )
        ],
        fills=[
            ExecutionFill(
                fill_id="binance:BTCUSDT:1",
                venue_order_id="venue-1",
                exchange=Exchange.BINANCE,
                environment=ExecutionEnvironment.TESTNET,
                symbol=submitted.symbol,
                side=submitted.side,
                quantity=submitted.quantity,
                price=100,
                occurred_at=observed_at,
                observed_at=observed_at,
            )
        ],
        balances=[
            VenueBalanceSnapshot(
                exchange=Exchange.BINANCE,
                environment=ExecutionEnvironment.TESTNET,
                asset="USDT",
                available=9_000,
                equity=9_000,
                observed_at=observed_at,
            ),
            VenueBalanceSnapshot(
                exchange=Exchange.BINANCE,
                environment=ExecutionEnvironment.TESTNET,
                asset="BTC",
                available=submitted.quantity,
                equity=submitted.quantity,
                observed_at=observed_at,
            ),
        ],
        observed_at=observed_at,
    )
    reconciler = ReconciliationService(
        adapter=adapter,
        risk_manager=risk,
        audit_service=audit,
        repository=repository,
    )
    run = await reconciler.reconcile_once()
    assert run.status == ReconciliationRunStatus.DRIFT
    stored = await repository.load_oms_order(queued.oms_order_id)
    assert stored.status == OMSOrderStatus.FILLED
    assert stored.terminal_at is not None
    fills = await repository.load_execution_fills(
        oms_order_id=queued.oms_order_id
    )
    assert [fill.fill_id for fill in fills] == ["binance:BTCUSDT:1"]
    latest = await repository.load_latest_reconciliation(
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
    )
    assert latest[0].run_id == run.run_id
    assert {balance.asset for balance in latest[3]} == {"BTC", "USDT"}
    assert risk.state.open_positions == 1
    await database.dispose()


async def test_orphan_venue_order_activates_durable_kill_switch(tmp_path):
    (
        database,
        repository,
        state_machine,
        audit,
        risk,
        _,
        adapter,
        _,
        _,
    ) = await oms_stack(tmp_path)
    adapter.snapshot = VenueStateSnapshot(
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
        orders=[
            VenueOrderSnapshot(
                exchange=Exchange.BINANCE,
                environment=ExecutionEnvironment.TESTNET,
                venue_order_id="orphan",
                client_order_id="foreign-client-order",
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                order_type=OMSOrderType.MARKET,
                status=OMSOrderStatus.SUBMITTED,
                quantity=1,
            )
        ],
    )
    reconciler = ReconciliationService(
        adapter=adapter,
        risk_manager=risk,
        audit_service=audit,
        repository=repository,
    )
    run = await reconciler.reconcile_once()
    assert run.critical_mismatch_count == 1
    assert risk.control_state.active
    assert state_machine.kill_switch_active
    await database.dispose()


async def test_orphan_venue_fill_activates_durable_kill_switch(tmp_path):
    (
        database,
        repository,
        state_machine,
        audit,
        risk,
        _,
        adapter,
        _,
        _,
    ) = await oms_stack(tmp_path)
    observed_at = datetime.now(timezone.utc)
    adapter.snapshot = VenueStateSnapshot(
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
        fills=[
            ExecutionFill(
                fill_id="binance:BTCUSDT:foreign-fill",
                venue_order_id="foreign-venue-order",
                client_order_id="foreign-client-order",
                exchange=Exchange.BINANCE,
                environment=ExecutionEnvironment.TESTNET,
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                quantity=0.01,
                price=100_000,
                occurred_at=observed_at,
                observed_at=observed_at,
            )
        ],
        observed_at=observed_at,
    )
    reconciler = ReconciliationService(
        adapter=adapter,
        risk_manager=risk,
        audit_service=audit,
        repository=repository,
    )
    run = await reconciler.reconcile_once()
    assert run.critical_mismatch_count == 1
    assert run.mismatch_count == 1
    latest = await repository.load_latest_reconciliation(
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
    )
    assert latest[1][0].mismatch_type == (
        ReconciliationMismatchType.ORPHAN_VENUE_FILL
    )
    assert risk.control_state.active
    assert state_machine.kill_switch_active
    await database.dispose()


def test_bybit_one_way_position_reconciliation_nets_closing_fills():
    observed_at = datetime.now(timezone.utc)
    buy = sample_oms_order(exchange=Exchange.BYBIT).model_copy(
        update={
            "status": OMSOrderStatus.FILLED,
            "cumulative_filled_quantity": 0.01,
            "average_fill_price": 100_000,
            "terminal_at": observed_at,
        }
    )
    sell = buy.model_copy(
        update={
            "oms_order_id": "closing-order",
            "client_order_id": "cc-" + ("b" * 32),
            "approval_id": "c" * 64,
            "side": OrderSide.SELL,
        }
    )
    snapshot = VenueStateSnapshot(
        exchange=Exchange.BYBIT,
        environment=ExecutionEnvironment.TESTNET,
        positions=[],
        observed_at=observed_at,
    )
    assert not _compare_positions(
        run_exchange=Exchange.BYBIT,
        environment=ExecutionEnvironment.TESTNET,
        local_orders=[buy, sell],
        snapshot=snapshot,
        run_id="test-run",
    )


def test_fill_is_correlated_through_matching_venue_order_snapshot():
    observed_at = datetime.now(timezone.utc)
    local = sample_oms_order()
    snapshot = VenueStateSnapshot(
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
        orders=[
            VenueOrderSnapshot(
                exchange=Exchange.BINANCE,
                environment=ExecutionEnvironment.TESTNET,
                venue_order_id="venue-discovered-after-ambiguous-write",
                client_order_id=local.client_order_id,
                symbol=local.symbol,
                side=local.side,
                order_type=local.order_type,
                status=OMSOrderStatus.FILLED,
                quantity=local.quantity,
                cumulative_filled_quantity=local.quantity,
                average_fill_price=local.reference_price,
                observed_at=observed_at,
            )
        ],
        fills=[
            ExecutionFill(
                fill_id="binance:BTCUSDT:discovered",
                venue_order_id=(
                    "venue-discovered-after-ambiguous-write"
                ),
                exchange=Exchange.BINANCE,
                environment=ExecutionEnvironment.TESTNET,
                symbol=local.symbol,
                side=local.side,
                quantity=local.quantity,
                price=local.reference_price,
                occurred_at=observed_at,
                observed_at=observed_at,
            )
        ],
        observed_at=observed_at,
    )
    mismatches, _ = _compare_orders([local], snapshot)
    assert ReconciliationMismatchType.ORPHAN_VENUE_FILL not in {
        mismatch.mismatch_type for mismatch in mismatches
    }


def test_month_7_contracts_and_migration_are_versioned_and_private():
    manifest = json.loads(
        (
            CONTRACT_ROOT.parent.parent / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    month_7 = {
        "oms-order.schema.json",
        "execution-command.schema.json",
        "venue-order-snapshot.schema.json",
        "execution-fill.schema.json",
        "venue-position-snapshot.schema.json",
        "venue-balance-snapshot.schema.json",
        "reconciliation-run.schema.json",
        "reconciliation-mismatch.schema.json",
    }
    for name in month_7:
        schema = json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert f"schemas/v1/{name}" in manifest["schemas"]

    migration = next(
        (
            CONTRACT_ROOT.parents[3] / "supabase" / "migrations"
        ).glob("*_create_oms_testnet_reconciliation.sql")
    ).read_text(encoding="utf-8")
    for table in (
        "oms_orders",
        "oms_order_events",
        "execution_commands",
        "execution_fills",
        "reconciliation_runs",
        "reconciliation_mismatches",
        "venue_position_snapshots",
        "venue_balance_snapshots",
    ):
        assert f"capital_cipher.{table}" in migration
        assert f"alter table capital_cipher.{table} enable row level security" in migration
    assert "security invoker" in migration.lower()
    assert "security definer" not in migration.lower()
    assert "from anon" in migration.lower()
    assert "from authenticated" in migration.lower()


def test_month_7_python_evidence_matches_published_contracts():
    now = datetime.now(timezone.utc)
    order = sample_oms_order()
    command = ExecutionCommand(
        oms_order_id=order.oms_order_id,
        command_type="SUBMIT",
    )
    venue_order = VenueOrderSnapshot(
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
        venue_order_id="venue",
        client_order_id=order.client_order_id,
        symbol=order.symbol,
        side=order.side,
        order_type=order.order_type,
        status=OMSOrderStatus.SUBMITTED,
        quantity=order.quantity,
    )
    fill = ExecutionFill(
        fill_id="fill",
        venue_order_id="venue",
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        price=order.reference_price,
        occurred_at=now,
    )
    position = VenuePositionSnapshot(
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
    )
    balance = VenueBalanceSnapshot(
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
        asset="USDT",
        available=1_000,
        equity=1_000,
    )
    run = ReconciliationRun(
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
        status=ReconciliationRunStatus.DRIFT,
        completed_at=now,
    )
    mismatch = ReconciliationMismatch(
        run_id=run.run_id,
        mismatch_type=ReconciliationMismatchType.ORDER_STATUS_DRIFT,
        severity=ReconciliationSeverity.WARNING,
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
    )
    documents = {
        "oms-order.schema.json": order,
        "execution-command.schema.json": command,
        "venue-order-snapshot.schema.json": venue_order,
        "execution-fill.schema.json": fill,
        "venue-position-snapshot.schema.json": position,
        "venue-balance-snapshot.schema.json": balance,
        "reconciliation-run.schema.json": run,
        "reconciliation-mismatch.schema.json": mismatch,
    }
    for name, model in documents.items():
        schema = json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))
        errors = list(
            Draft202012Validator(schema).iter_errors(
                model.model_dump(mode="json")
            )
        )
        assert errors == []


def test_api_exposes_no_oms_submission_endpoint():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "post" not in paths["/api/v1/oms/orders"]
    assert "post" in paths["/api/v1/oms/reconciliation/run"]
    assert "post" in paths["/api/v1/oms/orders/{oms_order_id}/cancel"]
