"""Offline boundary, temporal leakage and episodic evaluation tests."""
import asyncio
import hashlib
import socket
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.agents.intelligence.brain import Evidence, EvidenceStore, EpisodicMemory, PaperBrain
from app.tests.test_intelligence_reasoning import snapshot

T = datetime(2026, 10, 15, 15, tzinfo=timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network forbidden in intelligence tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def evidence(**changes):
    data = dict(source="fixture", source_event_id="event-1", provenance_uri="fixture://news/1",
                symbol="BTCUSDT", observed_at=T-timedelta(minutes=1), received_at=T,
                expires_at=T+timedelta(minutes=5), content="Untrusted external report",
                payload_sha256=hashlib.sha256(b"Untrusted external report").hexdigest())
    data.update(changes)
    return Evidence(**data)


class FakeProvider:
    name, model = "offline", "fixture-v1"

    def __init__(self, response=None, error=None, delay=0):
        self.response, self.error, self.delay = response, error, delay
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.response or ('{"bias":"BULLISH","summary":"Supplied evidence suggests upside",'
                                 '"counter_evidence":["Uncertain"],"invalidation":"Range breaks",'
                                 '"probability_up":0.7,"evidence_ids":[]}')


def test_evidence_integrity_time_and_scope():
    store = EvidenceStore()
    item = evidence()
    store.ingest(item)
    store.ingest(item)
    assert store.available("BTCUSDT", T) == (item,)
    assert not store.available("BTCUSDT", T-timedelta(seconds=1))
    assert not store.available("BTCUSDT", item.expires_at)
    assert not store.available("ETHUSDT", T)
    with pytest.raises(ValueError):
        store.ingest(evidence(provenance_uri="fixture://changed"))
    with pytest.raises(ValueError):
        evidence(payload_sha256="0"*64)
    with pytest.raises(ValueError):
        evidence(received_at=T-timedelta(days=1))
    with pytest.raises(ValueError):
        store.available("BTCUSDT", T.replace(tzinfo=None))


@pytest.mark.asyncio
async def test_model_episode_evaluation_and_historical_recall():
    store, memory, provider = EvidenceStore(), EpisodicMemory(), FakeProvider()
    store.ingest(evidence())
    store.ingest(evidence(source_event_id="late", received_at=T+timedelta(seconds=1)))
    brain = PaperBrain(store, memory, provider)
    episode = await brain.reason("one", snapshot(), as_of=T, horizon=timedelta(minutes=15))
    assert episode.status == "MODEL"
    assert len(provider.requests[0].evidence) == 1
    assert episode.assessment.action == "WAIT"
    assert not episode.assessment.order_authority
    target = T+timedelta(minutes=15)
    params = dict(symbol="BTCUSDT", timeframe="15m", observed_at=target,
                  received_at=target, price=90000, source_id="fixture-candle")
    outcome = memory.evaluate("one", **params)
    assert outcome.correct and outcome.brier_loss == pytest.approx(0.09)
    assert memory.evaluate("one", **params) == outcome
    assert memory.recall("BTCUSDT", T)[0][1] is None
    assert memory.recall("BTCUSDT", target)[0][1] == outcome
    assert not memory.recall("BTCUSDT", T-timedelta(seconds=1))
    for changes in [dict(symbol="ETHUSDT"), dict(timeframe="1h"),
                    dict(observed_at=target+timedelta(seconds=1)), dict(price=float("nan")),
                    dict(price=80000), dict(received_at=T)]:
        with pytest.raises(ValueError):
            memory.evaluate("one", **(params | changes))
    with pytest.raises(ValueError):
        await brain.reason("one", snapshot(), as_of=T, horizon=timedelta(minutes=15))
    assert len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["bad json", "{}", '{"action":"BUY"}',
    '{"bias":"BULLISH","summary":"x","counter_evidence":["x"],"invalidation":"x","evidence_ids":["invented"]}',
    '{"bias":"BULLISH","summary":"x","counter_evidence":["x"],"invalidation":"x","evidence_ids":[],"order_authority":true}',
    '{"bias":"BULLISH","summary":"x","counter_evidence":["x"],"invalidation":"x","evidence_ids":[],"probability_up":NaN}',
    "x"*32001])
async def test_invalid_model_output_falls_back(raw):
    brain = PaperBrain(EvidenceStore(), EpisodicMemory(), FakeProvider(raw))
    result = await brain.reason("one", snapshot(), as_of=T, horizon=timedelta(minutes=15))
    assert result.status == "FALLBACK" and result.failure == "invalid_response"
    assert result.assessment.action == "WAIT" and not result.assessment.decision_authority


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,failure", [
    (FakeProvider(error=RuntimeError("secret must never be saved")), "provider_error"),
    (FakeProvider(delay=1), "timeout"),
])
async def test_provider_failures_are_bounded_and_redacted(provider, failure):
    result = await PaperBrain(EvidenceStore(), EpisodicMemory(), provider, timeout_seconds=.01).reason(
        "one", snapshot(), as_of=T, horizon=timedelta(minutes=15))
    assert result.failure == failure
    assert "secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_paper_and_time_guards_before_provider_call():
    provider = FakeProvider()
    brain = PaperBrain(EvidenceStore(), EpisodicMemory(), provider)
    for changes in [dict(mode="LIVE"), dict(as_of=T-timedelta(seconds=1)),
                    dict(as_of=T.replace(tzinfo=None)), dict(horizon=timedelta(0))]:
        with pytest.raises(ValueError):
            await brain.reason("one", snapshot(), **(dict(as_of=T, horizon=timedelta(minutes=15)) | changes))
    assert not provider.requests
    result = await PaperBrain(EvidenceStore(), EpisodicMemory()).reason(
        "baseline", snapshot(), as_of=T, horizon=timedelta(minutes=15))
    assert result.status == "BASELINE" and result.assessment.probability_up is None


@pytest.mark.asyncio
async def test_valid_citations_are_exposed_to_adapter_and_preserved():
    store = EvidenceStore()
    item = evidence()
    store.ingest(item)
    import json
    provider = FakeProvider(json.dumps(dict(bias="MIXED", summary="Uncertain",
        counter_evidence=["Unverified source"], invalidation="Await confirmation",
        evidence_ids=[item.evidence_id])))
    memory = EpisodicMemory()
    result = await PaperBrain(store, memory, provider).reason(
        "cited", snapshot(), as_of=T, horizon=timedelta(minutes=15))
    assert result.status == "MODEL"
    assert provider.requests[0].evidence_ids == (item.evidence_id,)
    assert result.assessment.evidence_ids == (item.evidence_id,)
    with pytest.raises(ValueError):
        result.assessment.bias = "BULLISH"
    assert memory.append(result) == result
    with pytest.raises(ValueError):
        memory.append(result.model_copy(update={"model": "changed"}))
    target = T + timedelta(minutes=15)
    outcome = memory.evaluate("cited", symbol="BTCUSDT", timeframe="15m",
        observed_at=target, received_at=target+timedelta(minutes=1),
        price=snapshot().close, source_id="fixture")
    assert outcome.correct is None and outcome.brier_loss is None
    assert memory.recall("BTCUSDT", target)[0][1] is None


@pytest.mark.asyncio
async def test_cancellation_does_not_create_episode():
    memory = EpisodicMemory()
    brain = PaperBrain(EvidenceStore(), memory, FakeProvider(error=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await brain.reason("cancelled", snapshot(), as_of=T, horizon=timedelta(minutes=15))
    assert memory.get("cancelled") is None
