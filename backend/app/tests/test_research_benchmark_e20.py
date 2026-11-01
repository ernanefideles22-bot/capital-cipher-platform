"""E2.0 frozen historical benchmark safety regressions, no markets/network."""
from pathlib import Path

import pytest
from app.strategy.research_benchmark_e20 import (
    DATASETS, RESEARCH_PROTOCOL, benchmark_series, frozen_hypotheses,
    load_spot_csv, protocol_hash,
)
from scripts.run_e20_chart_skill_benchmark import run

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "market_samples"


def test_hypotheses_frozen_before_market_data_comparison():
    assert len(protocol_hash()) == 64
    assert RESEARCH_PROTOCOL["execution_permission"] is False
    assert RESEARCH_PROTOCOL["holdout_fraction"] == 0.40
    assert RESEARCH_PROTOCOL["cost_bps_per_side"] == [12.0, 25.0]
    for symbol, tf in DATASETS:
        baseline, candidate = frozen_hypotheses(symbol, tf)
        assert not baseline.decision_authority
        assert not candidate.decision_authority
        assert baseline.fingerprint != candidate.fingerprint
        assert baseline.side == candidate.side
        assert baseline.timeframe == tf


def test_real_csv_is_frozen_complete_and_disallows_future_bars():
    for symbol, tf in DATASETS:
        p = FIXTURES / f"e20_{symbol}_{tf}_binance_spot_20261031.csv"
        candles, manifest = load_spot_csv(p, symbol=symbol, timeframe=tf)
        assert len(candles) == 800
        assert not manifest["receipt_time_verified"]
        assert not manifest["order_book_spread_verified"]
        assert len(manifest["csv_sha256"]) == 64
        assert candles[-1].closed_at.isoformat() <= RESEARCH_PROTOCOL[
            "data_end_exclusive_utc"
        ]


def test_replay_results_do_not_promote_candidates():
    result = run(FIXTURES)
    assert not result["execution_authorized"]
    assert not result["profitability_consistency_proven"]
    assert result["deployed_agent_improved"] == "NOT_TESTED"
    assert len(result["benchmarks"]) == 3
    for benchmark in result["benchmarks"]:
        assert benchmark["mode"] == (
            "OFFLINE_INDEPENDENT_HISTORICAL_RESEARCH_ONLY"
        )
        assert not benchmark["current_deployed_agent_comparison_performed"]
        assert not benchmark["prospective_forward_validation_performed"]
        assert len(benchmark["comparisons"]) == 2
        for scenario in benchmark["comparisons"]:
            assert scenario["reference"]["data_hash"] == (
                scenario["candidate"]["data_hash"]
            )
            assert not scenario["positive_improvement_proven"]
            assert 0 <= scenario["reference"]["max_drawdown_percent"] <= 100
            assert 0 <= scenario["candidate"]["max_drawdown_percent"] <= 100


def test_tampered_noncontiguous_csv_fails_closed(tmp_path):
    symbol, tf = DATASETS[0]
    original = FIXTURES / f"e20_{symbol}_{tf}_binance_spot_20261031.csv"
    lines = original.read_text().splitlines()
    lines.pop(100)
    path = tmp_path / "missing.csv"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="sample length"):
        load_spot_csv(path, symbol=symbol, timeframe=tf)
    lines = original.read_text().splitlines()
    lines[110] = lines[109]
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="Missing, duplicate"):
        load_spot_csv(path, symbol=symbol, timeframe=tf)


def test_future_data_cannot_enter_frozen_sample(tmp_path):
    symbol, tf = DATASETS[0]
    original = FIXTURES / f"e20_{symbol}_{tf}_binance_spot_20261031.csv"
    lines = original.read_text().splitlines()
    step_ms = 900000
    future_start = 1793480000000
    for idx, line in enumerate(lines[1:], 1):
        cells = line.split(",")
        cells[0] = str(future_start + (idx - 1) * step_ms)
        lines[idx] = ",".join(cells)
    path = tmp_path / "future.csv"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="cutoff"):
        load_spot_csv(path, symbol=symbol, timeframe=tf)
