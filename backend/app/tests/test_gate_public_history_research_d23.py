"""D2.3 Gate public historical parser tests. No network or trade credentials."""
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.common import Exchange
from scripts.gate_public_history_research import (
    normalize_candles, normalize_funding, hypothesis, summarize,
    public_get, INTERVAL_SECONDS,
)


BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def candle_records(count=650, *, interval="15m", start=None):
    step = INTERVAL_SECONDS[interval]
    t0 = int((start or BASE).timestamp())
    return [
        {
            "t": t0 + i * step,
            "o": str(100 + 0.03 * i),
            "h": str(101 + 0.03 * i),
            "l": str(99 + 0.03 * i),
            "c": str(100.4 + 0.03 * i),
            "v": str(100 + i % 17),
            "sum": "100000",
        }
        for i in range(count)
    ]


def cutoff_for(records, interval):
    return datetime.fromtimestamp(
        records[-1]["t"] + INTERVAL_SECONDS[interval], timezone.utc,
    )


def test_gate_parser_keeps_gate_identity_and_rejects_future_open_bar():
    raw = candle_records()
    raw.append({**raw[-1], "t": raw[-1]["t"] + 900})
    cut = cutoff_for(raw[:-1], "15m")
    bars = normalize_candles("BTC_USDT", "15m", raw, cutoff=cut)
    assert len(bars) == 650
    assert bars[0].exchange == Exchange.GATE
    assert bars[0].symbol == "BTCUSDT"
    assert bars[-1].closed_at < cut
    assert bars[-1].received_at > bars[-1].closed_at
    assert bars[60].received_at > bars[60].closed_at


def test_gate_parser_rejects_ohlcv_gaps_duplicates_and_invalid_values():
    raw = candle_records()
    cut = cutoff_for(raw, "15m")
    with pytest.raises(ValueError, match="Duplicate"):
        normalize_candles("BTC_USDT", "15m", raw + [raw[4]], cutoff=cut)
    with pytest.raises(ValueError, match="nonconsecutive"):
        normalize_candles("BTC_USDT", "15m", raw[:200] + raw[201:], cutoff=cut)
    corrupted = [dict(row) for row in raw]
    corrupted[23]["c"] = "nan"
    with pytest.raises(ValueError, match="Non-finite"):
        normalize_candles("BTC_USDT", "15m", corrupted, cutoff=cut)


def test_gate_parser_rejects_short_history_and_misaligned_timestamps():
    raw = candle_records(590)
    with pytest.raises(ValueError, match="Insufficient"):
        normalize_candles(
            "BTC_USDT", "15m", raw, cutoff=cutoff_for(raw, "15m"),
        )
    raw = candle_records()
    raw[9]["t"] += 1
    with pytest.raises(ValueError, match="Misaligned"):
        normalize_candles(
            "BTC_USDT", "15m", raw, cutoff=cutoff_for(raw, "15m"),
        )


def test_gate_funding_is_explicit_and_not_backfilled():
    raw = [
        {"t": int(BASE.timestamp()) + 3600, "r": "0.0002"},
        {"t": int(BASE.timestamp()) - 3600, "r": "-0.0001"},
        {"t": int(BASE.timestamp()) + 7200, "r": "0.0003"},
    ]
    funds = normalize_funding(
        raw, BASE + timedelta(hours=1),
    )
    assert len(funds) == 2
    assert funds[0]["rate"] == -.0001
    assert funds[1]["rate"] == .0002
    with pytest.raises(ValueError, match="Duplicate"):
        normalize_funding([raw[0], raw[0]], BASE + timedelta(days=1))


def test_gate_parser_handles_all_three_timeframes_and_candidate_research():
    for contract, interval in (
        ("BTC_USDT", "15m"), ("ETH_USDT", "1h"), ("SOL_USDT", "4h")
    ):
        raw = candle_records(650, interval=interval)
        bars = normalize_candles(
            contract, interval, raw, cutoff=cutoff_for(raw, interval),
        )
        result = summarize(contract, interval, bars)
        assert result["rows"] == 650
        assert len(result["experiments"]) == 2
        assert all(not e["execution_authorized"] for e in result["experiments"])
        assert all(isinstance(e["later_net_usdt_12bps"], (int, float))
                   for e in result["experiments"])
        assert all(len(e["fingerprint"]) == 64 for e in result["experiments"])


def test_public_get_refuses_arbitrary_paths_and_markets():
    with pytest.raises(ValueError, match="allowlist"):
        public_get("/futures/usdt/orders", {"contract": "BTC_USDT"})
    with pytest.raises(ValueError, match="Unapproved"):
        public_get("/futures/usdt/candlesticks", {"contract": "DOGE_USDT"})


def test_gate_research_proposals_do_not_get_execution_authority():
    proposal = hypothesis("BTCUSDT", "15m", "BUY")
    assert proposal.decision_authority is False
    assert proposal.timeframe == "15m"
    assert proposal.symbol == "BTCUSDT"
