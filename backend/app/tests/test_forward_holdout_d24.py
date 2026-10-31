"""D2.4: preregistration and one-shot forward holdout, never live execution."""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.forward_holdout import (
    ForwardCampaign, ForwardHoldoutRegistry,
)
from app.strategy.research_lab import StrategyHypothesis


T0 = datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=15)
H_START = T0 + timedelta(days=2)
H_END = H_START + 72 * STEP


def proposal(side: str = "BUY"):
    return StrategyHypothesis(
        author_agent=f"ForwardResearch{side}",
        name=f"Frozen trend momentum {side}",
        family="MOMENTUM_REGIME",
        symbol="BTCUSDT", timeframe="15m", side=side,
        entry_features=(
            ("trend_up", "momentum_positive") if side == "BUY"
            else ("trend_down", "momentum_negative")
        ),
        invalidation_feature=("trend_down" if side == "BUY" else "trend_up"),
        parameters=(("atr_stop_multiple", 1.5),
                    ("target_r_multiple", 2.0)),
        thesis="Frozen directional trend hypothesis, one shot forward "
               "validation without any automatic trading permissions.",
    )


def campaign(*, candidates=None):
    return ForwardCampaign(
        campaign_id="D24-FORWARD-BTC-15M-20261102",
        frozen_commit_sha="a" * 40,
        training_dataset_sha256="b" * 64,
        training_ends_at=H_START - 3 * STEP,
        holdout_starts_at=H_START,
        holdout_ends_at=H_END,
        source_exchange="GATE",
        symbol="BTCUSDT", timeframe="15m",
        candidates=candidates or (proposal("BUY"), proposal("SELL")),
        candidate_budget=4,
        embargo_candles=2,
    )


def bars():
    out = []
    for i in range(60 + 72):
        start = H_START + (i - 60) * STEP
        base = 75000 + i * 15
        close_at = start + STEP - timedelta(milliseconds=1)
        out.append(Candle(
            exchange=Exchange.GATE, symbol="BTCUSDT", timeframe="15m",
            open=base, high=base + 25, low=base - 15,
            close=base + 10, volume=100 + i % 9,
            closed_at=close_at,
            received_at=close_at + timedelta(seconds=1),
        ))
    return out


def registry(tmp_path, clock):
    return ForwardHoldoutRegistry(
        tmp_path / "forward-evidence.db",
        signing_key=b"independent-runner-test-secret-32-bytes-long!",
        clock=lambda: clock[0],
    )


def test_frozen_campaign_runs_once_after_future_end(tmp_path):
    clock = [T0]
    r = registry(tmp_path, clock)
    p = campaign()
    digest = r.register(p)
    assert len(digest) == 64
    assert not r.consumed(p.campaign_id)
    with pytest.raises(ValueError, match="not fully complete"):
        r.evaluate_once(p, bars())
    clock[0] = H_END + STEP + timedelta(seconds=1)
    receipt = r.evaluate_once(p, bars())
    assert receipt.campaign_sha256 == digest
    assert receipt.status == "OFFLINE_RESEARCH_ONLY"
    assert not receipt.execution_authorized
    assert len(receipt.candidates) == 2
    assert all(not x.execution_authorized for x in receipt.candidates)
    assert r.consumed(p.campaign_id)
    assert r.verify_receipt(p)["data_sha256"] == receipt.data_sha256
    with pytest.raises(sqlite3.IntegrityError):
        r.evaluate_once(p, bars())


def test_registration_must_precede_holdout_and_freeze_every_hypothesis(tmp_path):
    clock = [H_START]
    reg = registry(tmp_path, clock)
    with pytest.raises(ValueError, match="before future holdout"):
        reg.register(campaign())
    clock[0] = T0
    reg.register(campaign())
    modified = campaign(candidates=(proposal("BUY"),))
    clock[0] = H_END + STEP
    with pytest.raises(ValueError, match="identity/integrity mismatch"):
        reg.evaluate_once(modified, bars())
    assert not reg.consumed(campaign().campaign_id)


def test_campaign_rejects_weak_embargo_unbounded_search_and_bad_source():
    p = campaign()
    with pytest.raises(ValueError, match="embargo"):
        replace(p, training_ends_at=H_START - STEP)
    with pytest.raises(ValueError, match="budget"):
        replace(p, candidate_budget=0)
    with pytest.raises(ValueError, match="exceed"):
        replace(p, candidate_budget=1)
    with pytest.raises(ValueError, match="Gate"):
        replace(p, source_exchange="BINANCE")
    with pytest.raises(ValueError, match="Repeated"):
        replace(p, candidates=(proposal("BUY"), proposal("BUY")))


def test_bad_data_consumes_once_no_peeking_or_retries(tmp_path):
    clock = [T0]
    reg = registry(tmp_path, clock)
    p = campaign()
    reg.register(p)
    clock[0] = H_END + STEP
    with pytest.raises(ValueError, match="exact 60 warmup"):
        reg.evaluate_once(p, bars()[:75])
    assert reg.consumed(p.campaign_id)
    with pytest.raises(ValueError, match="signed holdout"):
        reg.verify_receipt(p)
    with pytest.raises(sqlite3.IntegrityError):
        reg.evaluate_once(p, bars())


def test_bad_source_exchange_fails_closed_and_consumes(tmp_path):
    clock = [T0]
    reg = registry(tmp_path, clock)
    p = campaign()
    reg.register(p)
    bad = bars()
    bad[61] = bad[61].model_copy(update={"exchange": Exchange.BINANCE})
    clock[0] = H_END + STEP
    with pytest.raises(ValueError, match="Gate-identified"):
        reg.evaluate_once(p, bad)
    assert reg.consumed(p.campaign_id)


def test_valid_snapshot_signed_receipt_detects_tamper(tmp_path):
    clock = [T0]
    reg = registry(tmp_path, clock)
    p = campaign()
    reg.register(p)
    clock[0] = H_END + STEP
    reg.evaluate_once(p, bars())
    with sqlite3.connect(reg.path) as db:
        db.execute("DROP TRIGGER receipts_immutable_update")
        db.execute("UPDATE receipts SET receipt_json='{}'")
    with pytest.raises(ValueError, match="signed holdout"):
        reg.verify_receipt(p)


def test_holdout_rejects_missing_and_weak_key(tmp_path):
    with pytest.raises(ValueError, match="256-bit"):
        ForwardHoldoutRegistry(
            tmp_path / "unsafe.db", signing_key=b"known",
        )
    clock = [H_END + STEP]
    reg = registry(tmp_path, clock)
    with pytest.raises(ValueError, match="not preregistered"):
        reg.evaluate_once(campaign(), bars())
    assert not reg.consumed(campaign().campaign_id)
