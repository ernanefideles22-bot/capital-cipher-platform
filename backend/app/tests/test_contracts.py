"""Contract tests (docs/22): agents must respect input/output contracts."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.schemas.agents import AgentOutput
from app.schemas.common import AgentStatus, Signal
from app.schemas.data_catalog import CandleDatasetManifest
from app.schemas.decisions import Decision
from app.schemas.events import BusMessage
from app.schemas.market import Candle
from app.schemas.replay import ReplayCheckpoint
from app.market_data.clock import evaluate_clock_probe
from app.tests.conftest import make_candle

CONTRACT_ROOT = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "schemas" / "v1"


def load_contract(name: str) -> dict:
    schema = json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def test_agent_output_valid_contract():
    output = AgentOutput(
        agent_name="QuantAgent",
        status=AgentStatus.COMPLETED,
        signal=Signal.BUY,
        confidence=78,
        reason="test",
    )
    assert output.confidence == 78


@pytest.mark.parametrize("confidence", [-1, 101, 150])
def test_agent_output_confidence_bounds(confidence):
    with pytest.raises(ValidationError):
        AgentOutput(
            agent_name="QuantAgent",
            status=AgentStatus.COMPLETED,
            signal=Signal.BUY,
            confidence=confidence,
            reason="test",
        )


def test_agent_output_rejects_invalid_signal():
    with pytest.raises(ValidationError):
        AgentOutput(
            agent_name="QuantAgent",
            status=AgentStatus.COMPLETED,
            signal="MOON",
            confidence=50,
            reason="test",
        )


def test_decision_requires_correlation_id():
    with pytest.raises(ValidationError):
        Decision(
            symbol="BTCUSDT",
            timeframe="15m",
            candidate_action="BUY",
            confidence=80,
        )


def test_candle_invariant_high_low():
    with pytest.raises(ValidationError):
        Candle(
            exchange="BINANCE",
            symbol="BTCUSDT",
            timeframe="15m",
            open=100,
            high=90,  # high < open: invalid
            low=95,
            close=99,
            volume=10,
            closed_at="2026-07-01T12:00:00Z",
        )


def test_candle_rejects_negative_volume():
    with pytest.raises(ValidationError):
        Candle(
            exchange="BINANCE",
            symbol="BTCUSDT",
            timeframe="15m",
            open=100,
            high=101,
            low=99,
            close=100,
            volume=-1,
            closed_at="2026-07-01T12:00:00Z",
        )


def test_python_candle_matches_published_v1_contract():
    candle = make_candle()
    validator = Draft202012Validator(load_contract("market-candle.schema.json"))
    assert list(validator.iter_errors(candle.model_dump(mode="json"))) == []


def test_bus_message_matches_published_v1_contract():
    message = BusMessage(
        correlation_id=str(uuid4()),
        topic="market.events.v1",
        event_type="CANDLE_CLOSED",
        source="contract-test",
        payload={"symbol": "BTCUSDT"},
    )
    validator = Draft202012Validator(load_contract("event-envelope.schema.json"))
    assert list(validator.iter_errors(message.model_dump(mode="json"))) == []


def test_replay_checkpoint_matches_published_v1_contract():
    checkpoint = ReplayCheckpoint(
        replay_id="contract-replay",
        consumer_name="market-replay",
        topic="market.replay.v1",
        dataset_hash="a" * 64,
    )
    validator = Draft202012Validator(load_contract("replay-checkpoint.schema.json"))
    assert list(validator.iter_errors(checkpoint.model_dump(mode="json"))) == []


def test_dataset_manifest_matches_published_v1_contract():
    candle = make_candle()
    manifest = CandleDatasetManifest(
        dataset_id=f"candles:v1:{'a' * 64}",
        dataset_hash="a" * 64,
        exchange=candle.exchange,
        symbol=candle.symbol,
        timeframe=candle.timeframe,
        start_at=candle.closed_at,
        end_at=candle.closed_at,
        row_count=1,
        selection={"order": ["closed_at"]},
        quality_summary={"status": "VALID"},
    )
    validator = Draft202012Validator(load_contract("dataset-manifest.schema.json"))
    assert list(validator.iter_errors(manifest.model_dump(mode="json"))) == []


def test_clock_observation_matches_published_v1_contract():
    from datetime import datetime, timedelta, timezone

    started = datetime.now(timezone.utc)
    observation = evaluate_clock_probe(
        source="binance.server-time",
        request_started_at=started,
        source_at=started + timedelta(milliseconds=50),
        response_received_at=started + timedelta(milliseconds=100),
    )
    validator = Draft202012Validator(load_contract("clock-observation.schema.json"))
    assert list(validator.iter_errors(observation.model_dump(mode="json"))) == []
