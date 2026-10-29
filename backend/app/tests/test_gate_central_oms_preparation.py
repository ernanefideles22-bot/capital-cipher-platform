"""End-to-end local OMS approval consumption using no exchange writes."""
import httpx
import pytest

from app.audit.service import AuditService
from app.core.state_machine import SystemState, SystemStateMachine
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.execution.adapters.gate_testnet_metadata import GateTestnetMetadata
from app.execution.adapters.gate_testnet_preparation import GateTestnetPreparationAdapter
from app.oms.service import OMSService
from app.paper_trading.engine import PaperTradingEngine
from app.risk.venue_aware import VenueAwareRiskManager
from app.schemas.common import CandidateAction, Exchange
from app.schemas.oms import ExecutionEnvironment
from app.schemas.risk import RiskLimits
from app.tests.conftest import make_decision


@pytest.mark.asyncio
async def test_central_approval_queues_prepared_gate_order_once(tmp_path):
    database=Database(f"sqlite+aiosqlite:///{tmp_path/'gate-oms.db'}")
    await database.create_all()
    repository=Repository(database)
    sm=SystemStateMachine()
    await sm.transition(SystemState.INITIALIZING,reason="test",actor="test")
    await sm.transition(SystemState.PAPER,reason="test",actor="test")
    audit=AuditService(repository=repository)
    risk=VenueAwareRiskManager(RiskLimits(),sm,audit,repository=repository,
        execution_environment=ExecutionEnvironment.TESTNET,testnet_exchange=Exchange.GATE)
    async def equity(): return 10000.0
    risk.set_testnet_equity_provider(equity)
    await risk.initialize()
    paper=PaperTradingEngine(audit,risk,repository=repository)
    calls=[]
    def metadata_handler(request):
        calls.append(request)
        assert request.method=="GET" and request.url.host=="api-testnet.gateapi.io"
        return httpx.Response(200,json={"name":"BTC_USDT","quanto_multiplier":"0.0001",
            "order_size_min":"1","order_size_max":"100000","in_delisting":False})
    provider=GateTestnetMetadata(transport=httpx.MockTransport(metadata_handler),clock=lambda:1000)
    adapter=GateTestnetPreparationAdapter(provider,clock=lambda:1001)
    async def release(): return True
    service=OMSService(target_environment=ExecutionEnvironment.TESTNET,target_exchange=Exchange.GATE,
        paper_engine=paper,risk_manager=risk,audit_service=audit,
        adapters={(Exchange.GATE,ExecutionEnvironment.TESTNET):adapter},repository=repository,release_guard=release)
    decision=make_decision(CandidateAction.BUY)
    check=await risk.check(decision,entry_price=100,atr=1)
    assert check.approved
    first=await service.submit_approved(decision,check,current_price=100)
    second=await service.submit_approved(decision,check,current_price=100)
    assert first.oms_order_id==second.oms_order_id
    assert first.client_order_id.startswith("t-") and len(first.client_order_id)==30
    assert first.quantity*100 <= check.position_size + 1e-8
    assert len(calls)==1 and calls[0].method=="GET"
    assert not await adapter.healthcheck()
    await provider.aclose()
    await database.dispose()
