"""Month 9 invariants retained by the expanded Month 10 cohort."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator
from sqlalchemy import event, text
from sqlalchemy.exc import DatabaseError as SQLAlchemyDatabaseError

from app.agents.month9_specialists import (
    AUTOCORRELATION_DEFINITIONS,
    DRAWDOWN_DEFINITIONS,
    LIQUIDITY_DEFINITIONS,
    MONTH9_DIAGNOSTIC_DEFINITIONS,
    RANGE_EFFICIENCY_DEFINITIONS,
    RETURN_DEFINITIONS,
    TAIL_BALANCE_DEFINITIONS,
    VOLATILITY_DEFINITIONS,
    VOLUME_PRESSURE_DEFINITIONS,
)
from app.api.context import build_context
from app.core.config import Settings
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.main import create_app
from app.orchestrator.portfolio_consensus import (
    ConsensusExperimentService,
    DriftMonitor,
    PortfolioConstructionService,
    WeightedConsensusService,
)
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, CandidateAction, Signal, utcnow
from app.schemas.decisions import Decision
from app.schemas.portfolio_consensus import (
    ConsensusExperiment,
    ConsensusExperimentEvent,
    DriftObservation,
    WeightedConsensus,
)
from app.schemas.risk import RiskLimits
from app.schemas.specialist_evaluation import AgentScorecard
from app.tests.conftest import make_series

CONTRACT_ROOT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "contracts"
    / "schemas"
    / "v1"
)


def validate_contract(name: str, document: dict) -> None:
    validator = Draft202012Validator(
        json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))
    )
    assert list(validator.iter_errors(document)) == []


class EvaluationStub:
    def __init__(self, cards, history=None) -> None:
        self._cards = cards
        self._history = history or {}

    async def scorecards(self):
        return self._cards

    async def settled_history(self, *, agent_name, agent_version, limit=10_000):
        return self._history.get((agent_name, agent_version), [])[-limit:]


def baseline(action=CandidateAction.BUY, confidence=80) -> Decision:
    return Decision(
        correlation_id="month9-correlation",
        symbol="BTCUSDT",
        timeframe="15m",
        candidate_action=action,
        confidence=confidence,
        strategy="SCALP_15M:v1",
        reason="Static primary decision",
        agent_summary=[],
    )


def insufficient_consensus(experiment, decision) -> WeightedConsensus:
    return WeightedConsensus(
        correlation_id=decision.correlation_id,
        experiment_id=experiment.experiment_id,
        experiment_version=experiment.version,
        mode=experiment.mode,
        symbol=decision.symbol,
        timeframe=decision.timeframe,
        status="INSUFFICIENT_DATA",
        baseline_action=decision.candidate_action,
        baseline_confidence=decision.confidence,
        eligible_agent_count=0,
        final_action=decision.candidate_action,
        final_confidence=decision.confidence,
        reason="Static decision preserved",
    )


def test_month9_catalog_remains_bounded_in_300_agent_runtime():
    assert len(RETURN_DEFINITIONS) == 8
    assert len(VOLATILITY_DEFINITIONS) == 8
    assert len(DRAWDOWN_DEFINITIONS) == 6
    assert len(VOLUME_PRESSURE_DEFINITIONS) == 6
    assert len(RANGE_EFFICIENCY_DEFINITIONS) == 6
    assert len(TAIL_BALANCE_DEFINITIONS) == 5
    assert len(AUTOCORRELATION_DEFINITIONS) == 5
    assert len(LIQUIDITY_DEFINITIONS) == 6
    assert len(MONTH9_DIAGNOSTIC_DEFINITIONS) == 50
    assert len({item.name for item in MONTH9_DIAGNOSTIC_DEFINITIONS}) == 50

    context = build_context(Settings(), with_database=False)
    registrations = context.agent_registry.registrations()
    month9_names = {item.name for item in MONTH9_DIAGNOSTIC_DEFINITIONS}
    assert len(registrations) == 300
    assert sum(item.decision_role == "PRIMARY" for item in registrations) == 3
    assert sum(item.decision_role == "SHADOW" for item in registrations) == 297
    assert month9_names.issubset(
        {item.agent_name for item in registrations}
    )
    for agent in context.agent_registry.shadow_agents():
        assert not hasattr(agent, "_risk")
        assert not hasattr(agent, "_credentials")


async def test_month9_agents_are_deterministic_read_only_diagnostics():
    context = build_context(Settings(), with_database=False)
    for candle in make_series([100 * (1.001**i) for i in range(200)]):
        context.candle_store.add(candle)
    for definition in MONTH9_DIAGNOSTIC_DEFINITIONS:
        agent = context.agent_registry.get(definition.name)
        request = AgentInput(
            correlation_id="month9-diagnostic",
            agent_name=agent.name,
            symbol="BTCUSDT",
            timeframe="15m",
            market_context={"exchange": "BINANCE"},
        )
        first = await agent.run(request)
        second = await agent.run(request)
        assert first.status == AgentStatus.COMPLETED
        assert first.signal == second.signal
        assert first.confidence == second.confidence
        assert first.evidence["metric"] == second.evidence["metric"]
        assert first.evidence["read_only"] is True
        assert first.evidence["decision_authority"] is False


async def test_performance_consensus_requires_100_samples_and_never_reverses():
    context = build_context(Settings(), with_database=False)
    names = [item.name for item in MONTH9_DIAGNOSTIC_DEFINITIONS[:5]]
    registrations = {
        item.agent_name: item
        for item in context.agent_registry.registrations()
        if item.agent_name in names
    }
    cards = [
        AgentScorecard(
            agent_name=name,
            agent_version=registrations[name].version,
            sample_count=120,
            directional_sample_count=120,
            accuracy=0.65,
            mean_brier_loss=0.20,
            mean_marginal_contribution=0.01,
            status="EVALUATED",
            minimum_samples=30,
        )
        for name in names
    ]
    evaluation = EvaluationStub(cards)
    experiments = ConsensusExperimentService()
    experiment = ConsensusExperiment(
        name="confirmation_candidate",
        version="1.0.0",
        mode="CONFIRMATION",
        created_by="test-suite",
    )
    await experiments.register(experiment)
    activation = ConsensusExperimentEvent(
        experiment_id=experiment.experiment_id,
        event_type="ACTIVATED",
        actor="test-suite",
        reason="Exercise conservative confirmation overlay",
    )
    await experiments.record_event(activation)
    validate_contract(
        "consensus-experiment.schema.json",
        experiment.model_dump(mode="json"),
    )
    validate_contract(
        "consensus-experiment-event.schema.json",
        activation.model_dump(mode="json"),
    )
    drift = DriftMonitor(evaluation)
    service = WeightedConsensusService(
        evaluation,
        experiments,
        drift,
    )
    decision = baseline(CandidateAction.BUY, 80)
    outputs = [
        AgentOutput(
            agent_name=name,
            status=AgentStatus.COMPLETED,
            signal=Signal.SELL,
            confidence=80,
            reason="Synthetic eligible out-of-sample signal",
        )
        for name in names
    ]
    consensus = await service.evaluate(
        baseline=decision,
        outputs=outputs,
        registrations=registrations,
    )
    assert consensus.status == "READY"
    assert consensus.applied is True
    assert consensus.signal == Signal.SELL
    assert consensus.final_action == CandidateAction.WAIT
    assert consensus.final_confidence == 0
    assert sum(item.weight for item in consensus.weights) == pytest.approx(1)
    assert max(item.weight for item in consensus.weights) <= 0.25
    validate_contract(
        "weighted-consensus.schema.json",
        consensus.model_dump(mode="json"),
    )

    low_sample = EvaluationStub(
        [
            card.model_copy(
                update={
                    "sample_count": 99,
                    "directional_sample_count": 99,
                    "status": "EVALUATED",
                    "minimum_samples": 30,
                }
            )
            for card in cards
        ]
    )
    unavailable = await WeightedConsensusService(
        low_sample,
        experiments,
        DriftMonitor(low_sample),
    ).evaluate(
        baseline=decision,
        outputs=outputs,
        registrations=registrations,
    )
    assert unavailable.status == "INSUFFICIENT_DATA"
    assert unavailable.final_action == CandidateAction.BUY
    assert unavailable.final_confidence == 80


async def test_shadow_experiment_cannot_change_primary_decision():
    context = build_context(Settings(), with_database=False)
    name = MONTH9_DIAGNOSTIC_DEFINITIONS[0].name
    registration = context.agent_registry.get(name).registration()
    card = AgentScorecard(
        agent_name=name,
        agent_version=registration.version,
        sample_count=120,
        directional_sample_count=120,
        accuracy=0.70,
        mean_brier_loss=0.10,
        mean_marginal_contribution=0.02,
        status="EVALUATED",
        minimum_samples=30,
    )
    evaluation = EvaluationStub([card])
    experiments = ConsensusExperimentService()
    # The default minimum cohort is five, so this also exercises safe fallback.
    result = await WeightedConsensusService(
        evaluation,
        experiments,
        DriftMonitor(evaluation),
    ).evaluate(
        baseline=baseline(),
        outputs=[
            AgentOutput(
                agent_name=name,
                status=AgentStatus.COMPLETED,
                signal=Signal.SELL,
                confidence=100,
                reason="Shadow-only disagreement",
            )
        ],
        registrations={name: registration},
    )
    assert result.mode == "SHADOW"
    assert result.applied is False
    assert result.final_action == CandidateAction.BUY
    assert result.final_confidence == result.baseline_confidence


async def test_critical_drift_is_reproducible_and_excludes_agent():
    context = build_context(Settings(), with_database=False)
    name = MONTH9_DIAGNOSTIC_DEFINITIONS[0].name
    registration = context.agent_registry.get(name).registration()
    now = utcnow()
    history = []
    for index in range(70):
        recent = index >= 50
        outcome = SimpleNamespace(
            correct=False if recent else True,
            brier_loss=0.45 if recent else 0.10,
            marginal_contribution=-0.05 if recent else 0.03,
            realized_at=now + timedelta(minutes=15 * index),
            outcome_id=f"{index:064x}",
        )
        history.append((None, outcome))
    evaluation = EvaluationStub(
        [],
        {(name, registration.version): history},
    )
    monitor = DriftMonitor(evaluation)
    experiment = ConsensusExperimentService().active()
    observations = await monitor.observe(
        experiment,
        {name: registration},
    )
    observation = observations[(name, registration.version)]
    assert observation.severity == "CRITICAL"
    assert "ACCURACY_DROP" in observation.reasons
    assert "BRIER_LOSS_INCREASE" in observation.reasons
    assert "MARGINAL_CONTRIBUTION_DROP" in observation.reasons
    validate_contract(
        "drift-observation.schema.json",
        observation.model_dump(mode="json"),
    )


async def test_portfolio_cap_only_tightens_central_risk(
    risk_manager,
):
    experiment = ConsensusExperimentService().active()
    decision = baseline(confidence=80)
    consensus = insufficient_consensus(experiment, decision)
    service = PortfolioConstructionService(
        RiskLimits(),
        risk_manager,
        max_target_weight_percent=25,
    )
    proposal = await service.propose(
        decision=decision,
        consensus=consensus,
        balance=10_000,
    )
    assert proposal.status == "PROPOSED"
    assert proposal.requested_weight_percent == 20
    assert proposal.capped_weight_percent == 20
    assert proposal.max_notional == 2_000
    assert proposal.decision_authority is False
    validate_contract(
        "portfolio-proposal.schema.json",
        proposal.model_dump(mode="json"),
    )

    risk_check = await risk_manager.check(
        decision,
        entry_price=100,
        balance=10_000,
        max_notional_override=proposal.max_notional,
    )
    assert risk_check.approved is True
    assert risk_check.position_size == 2_000
    assert "PORTFOLIO_CONSTRUCTION_CAP" in risk_check.warnings
    assert risk_check.effective_limits["portfolio_max_notional"] == 2_000


async def test_month9_sqlite_artifacts_are_idempotent_and_append_only(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'month9.db'}")
    await database.create_all()
    repository = Repository(database)
    experiments = ConsensusExperimentService(repository)
    await experiments.initialize()
    experiment = experiments.active()
    decision = baseline()
    consensus = insufficient_consensus(experiment, decision)
    assert await repository.save_weighted_consensus(consensus) == consensus
    assert await repository.save_weighted_consensus(consensus) == consensus

    context = build_context(Settings(), with_database=False)
    construction = PortfolioConstructionService(
        RiskLimits(),
        context.risk_manager,
        repository,
    )
    proposal = await construction.propose(
        decision=decision,
        consensus=consensus,
        balance=10_000,
    )
    observation = DriftObservation(
        experiment_id=experiment.experiment_id,
        agent_name=MONTH9_DIAGNOSTIC_DEFINITIONS[0].name,
        agent_version="1.0.0",
        reference_samples=50,
        current_samples=20,
        reference_accuracy=0.60,
        current_accuracy=0.60,
        accuracy_delta=0,
        reference_brier_loss=0.20,
        current_brier_loss=0.20,
        brier_delta=0,
        reference_marginal_contribution=0.01,
        current_marginal_contribution=0.01,
        marginal_delta=0,
        severity="NONE",
        observed_at=utcnow(),
    )
    await repository.save_drift_observation(observation)
    second_observation = DriftObservation(
        **observation.model_dump(
            mode="python",
            exclude={
                "observation_id",
                "agent_name",
                "created_at",
            },
        ),
        agent_name=MONTH9_DIAGNOSTIC_DEFINITIONS[1].name,
    )
    await repository.save_drift_observations([second_observation])
    repeated_observation = DriftObservation(
        **observation.model_dump(
            mode="python",
            exclude={"observation_id", "created_at"},
        ),
        created_at=observation.created_at + timedelta(seconds=1),
    )
    repeated_second_observation = DriftObservation(
        **second_observation.model_dump(
            mode="python",
            exclude={"observation_id", "created_at"},
        ),
        created_at=second_observation.created_at + timedelta(seconds=1),
    )
    assert repeated_observation.observation_id == observation.observation_id
    select_statements: list[str] = []

    def capture_selects(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        if statement.lstrip().upper().startswith("SELECT"):
            select_statements.append(statement)

    event.listen(
        database.engine.sync_engine,
        "before_cursor_execute",
        capture_selects,
    )
    try:
        assert await repository.save_drift_observations(
            [repeated_observation, repeated_second_observation]
        ) == [observation, second_observation]
        assert sum(
            "drift_observations" in statement
            for statement in select_statements
        ) == 1
    finally:
        event.remove(
            database.engine.sync_engine,
            "before_cursor_execute",
            capture_selects,
        )
    assert (await repository.list_portfolio_proposals())[0] == proposal
    assert {
        item.observation_id
        for item in await repository.list_drift_observations()
    } == {
        observation.observation_id,
        second_observation.observation_id,
    }

    async with database.engine.begin() as connection:
        with pytest.raises(SQLAlchemyDatabaseError, match="append-only"):
            await connection.execute(
                text(
                    "UPDATE weighted_consensus_snapshots "
                    "SET status = 'READY'"
                )
            )
    await database.dispose()


async def test_month9_governance_apis_are_admin_only(tmp_path):
    admin_key = "g" * 32
    settings = Settings(
        ADMIN_API_KEY=admin_key,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'month9-api.db'}",
        AGENT_WORKER_ENABLED=False,
        BACKFILL_WORKER_ENABLED=False,
        OMS_WORKER_ENABLED=False,
        OMS_RECONCILIATION_ENABLED=False,
    )
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            denied = await client.get("/api/v1/governance/experiments")
            allowed = await client.get(
                "/api/v1/governance/experiments",
                headers={"X-API-Key": admin_key},
            )
            proposals = await client.get(
                "/api/v1/governance/portfolio-proposals",
                headers={"X-API-Key": admin_key},
            )
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["data"]["active_experiment_id"]
    assert proposals.json()["data"]["order_authority"] is False


def test_month9_contracts_and_private_migration_are_complete():
    manifest = json.loads(
        (CONTRACT_ROOT.parent.parent / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    names = (
        "consensus-experiment.schema.json",
        "consensus-experiment-event.schema.json",
        "weighted-consensus.schema.json",
        "drift-observation.schema.json",
        "portfolio-proposal.schema.json",
    )
    assert len(manifest["schemas"]) == 56
    for name in names:
        assert f"schemas/v1/{name}" in manifest["schemas"]
        Draft202012Validator.check_schema(
            json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))
        )
    migration = (
        Path(__file__).resolve().parents[3]
        / "supabase"
        / "migrations"
        / "20260720184831_create_portfolio_consensus_drift_governance.sql"
    ).read_text(encoding="utf-8")
    lowered = migration.lower()
    assert "enable row level security" in lowered
    assert "security invoker" in lowered
    assert "from authenticated" in lowered
    assert "from anon" in lowered
    assert "reject_portfolio_consensus_mutation" in migration
    assert "live" not in lowered
