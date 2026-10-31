"""D1: isolation, immutability and integrity tests for research memory."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.schemas.backtest import (
    BacktestExecutionAssumptions, BacktestMarginAssumptions,
    BacktestRequest, WalkForwardRequest,
)
from app.strategy.research_experiments import BaselineResearchRunner
from app.strategy.research_lab import StrategyHypothesis
from app.strategy.research_memory import ResearchMemory


def hypothesis():
    return StrategyHypothesis(
        author_agent="ShadowResearchAgent",
        name="Trend pullback research",
        family="CUSTOM_COMBINATION",
        symbol="BTCUSDT", timeframe="15m", side="BUY",
        entry_features=("trend_up", "pullback_ema20"),
        invalidation_feature="trend_down",
        parameters=(("atr_stop_multiple", 1.5),),
        thesis="Check whether positive directional context predicts continuation after costs.",
    )


def test_hypothesis_record_persists_and_is_idempotent(tmp_path):
    path = tmp_path / "audit.sqlite3"
    memory = ResearchMemory(path)
    p = hypothesis()
    event = memory.register(p)
    assert event.sequence == 1
    assert memory.register(p) == event
    assert len(ResearchMemory(path).list_events()) == 1
    assert ResearchMemory(path).verify_chain() == event.event_hash


def test_research_memory_rejects_conflicting_id_and_mutation(tmp_path):
    memory = ResearchMemory(tmp_path / "log.db")
    p = hypothesis()
    memory.register(p)
    with pytest.raises(ValueError, match="conflict"):
        memory.append(
            kind="RESEARCH_NOTE", hypothesis_hash=p.fingerprint,
            event_id="hypothesis:" + p.fingerprint,
            payload={"different": True},
        )
    with sqlite3.connect(memory.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE research_events SET kind='RESEARCH_NOTE' WHERE sequence=1")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM research_events WHERE sequence=1")


def test_research_memory_detects_tampered_database(tmp_path):
    memory = ResearchMemory(tmp_path / "log.db")
    event = memory.register(hypothesis())
    with sqlite3.connect(memory.path) as db:
        db.execute("DROP TRIGGER research_events_no_update")
        db.execute("UPDATE research_events SET payload_json=? WHERE sequence=1",
                   ('{"manipulated":true}',))
    with pytest.raises(ValueError, match="integrity"):
        memory.verify_chain(anchored_head=event.event_hash)


def test_research_memory_rejects_head_replacement(tmp_path):
    memory = ResearchMemory(tmp_path / "log.db")
    memory.register(hypothesis())
    with pytest.raises(ValueError, match="checkpoint"):
        memory.verify_chain(anchored_head="0" * 64)


def test_paper_observation_preserves_outcome_without_promoting(tmp_path):
    memory = ResearchMemory(tmp_path / "log.db")
    p = hypothesis()
    memory.register(p)
    entry = memory.paper_closed_observation(
        p, paper_order_id="paper-1", decision_id="decision-1",
        net_pnl=-12.5, exit_reason="STOP_LOSS",
        closed_at=datetime.now(timezone.utc), attribution_verified=False,
    )
    assert entry.payload["net_pnl"] == -12.5
    assert entry.payload["attribution_verified"] is False
    assert entry.payload["execution_authorized"] is False
    assert memory.verify_chain() == entry.event_hash


def test_paper_observation_rejects_invalid_records(tmp_path):
    memory = ResearchMemory(tmp_path / "log.db")
    p = hypothesis()
    with pytest.raises(ValueError, match="Non-finite"):
        memory.paper_closed_observation(
            p, paper_order_id="paper-1", decision_id="decision-1",
            net_pnl=float("nan"), exit_reason="STOP_LOSS",
            closed_at=datetime.now(timezone.utc), attribution_verified=False,
        )
    with pytest.raises(ValueError, match="timezone"):
        memory.paper_closed_observation(
            p, paper_order_id="paper-1", decision_id="decision-1",
            net_pnl=0, exit_reason="STOP_LOSS",
            closed_at=datetime(2026, 1, 1), attribution_verified=False,
        )


def test_observations_require_registered_hypothesis(tmp_path):
    memory = ResearchMemory(tmp_path / "log.db")
    with pytest.raises(ValueError, match="Register hypothesis"):
        memory.append(
            kind="RESEARCH_NOTE",
            hypothesis_hash=hypothesis().fingerprint,
            event_id="research:unregistered",
            payload={"note": "candidate unregistered"},
        )


def test_paper_attribution_cannot_be_self_attested(tmp_path):
    memory = ResearchMemory(tmp_path / "log.db")
    p = hypothesis()
    memory.register(p)
    with pytest.raises(ValueError, match="not implemented"):
        memory.paper_closed_observation(
            p, paper_order_id="paper-2", decision_id="decision-2",
            net_pnl=10.0, exit_reason="TAKE_PROFIT",
            closed_at=datetime.now(timezone.utc),
            attribution_verified=True,
        )


class FakeBacktester:
    def __init__(self, fee_bps=12.0):
        self.fee_bps = fee_bps

    def resolve_execution_assumptions(self, request):
        return BacktestExecutionAssumptions(
            taker_fee_bps=self.fee_bps, half_spread_bps=0,
            base_slippage_bps=0,
        )

    def resolve_strategy_version(self, request):
        return "SCALP_15M_v1"


def request():
    return WalkForwardRequest(
        backtest=BacktestRequest(
            symbol="BTCUSDT", timeframe="15m", source="inline",
            margin=BacktestMarginAssumptions(leverage=1),
        ),
        candidate_version="SCALP_15M_v1",
    )


@pytest.mark.asyncio
async def test_baseline_comparison_is_not_a_candidate_backtest(tmp_path, monkeypatch):
    import app.strategy.research_experiments as experiment_module
    class DummyWalkForward:
        def __init__(self, backtester, repository=None):
            assert repository is None

        async def run(self, request, candles):
            return SimpleNamespace(
                artifact_hash="a" * 64,
                dataset_hash="b" * 64, folds=[1, 2, 3],
                experiment_id="walk-forward:v2:" + "c" * 64,
                candidate_version="SCALP_15M_v1",
                validation_gate=SimpleNamespace(passed=True),
                test_gate=SimpleNamespace(passed=False),
                test_aggregate=SimpleNamespace(total_trades=25),
            )

    monkeypatch.setattr(experiment_module, "WalkForwardEngine", DummyWalkForward)
    monkeypatch.setattr(experiment_module, "walk_forward_artifact_hash",
                        lambda report: "a" * 64)
    mem = ResearchMemory(tmp_path / "log.db")
    result = await BaselineResearchRunner(
        journal=mem, backtester=FakeBacktester()
    ).run(hypothesis(), request(), [object()])
    assert result.validation_passed is True
    assert result.test_passed is False
    assert result.conclusion == "BASELINE_ONLY_NOT_HYPOTHESIS_EDGE"
    assert result.execution_authorized is False
    events = mem.list_events()
    assert len(events) == 2
    assert events[-1].kind == "BASELINE_WALK_FORWARD_OBSERVED"
    assert events[-1].payload["execution_authorized"] is False
    assert mem.verify_chain() == events[-1].event_hash


@pytest.mark.asyncio
async def test_unrealistically_low_costs_block_experiment(tmp_path):
    mem = ResearchMemory(tmp_path / "log.db")
    with pytest.raises(ValueError, match="12 bps"):
        await BaselineResearchRunner(
            journal=mem, backtester=FakeBacktester(fee_bps=2)
        ).run(hypothesis(), request(), [object()])
    assert mem.list_events() == []


@pytest.mark.asyncio
async def test_unreviewed_mismatched_candidate_is_not_assigned_edge(tmp_path):
    mem = ResearchMemory(tmp_path / "log.db")
    invalid = request().model_copy(update={"candidate_version": "UNKNOWN_v1"})
    with pytest.raises(ValueError, match="baseline"):
        await BaselineResearchRunner(
            journal=mem, backtester=FakeBacktester()
        ).run(hypothesis(), invalid, [object()])
    assert mem.list_events() == []
