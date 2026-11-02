"""Economic counterfactuals, path reconciliation and immutable research inputs."""
from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from app.schemas.common import Exchange
from app.strategy import gate_s2_replay as replay
from app.strategy import gate_s2c_confluence as confluence
from app.strategy import gate_s3_economic_diagnostic as economics
from app.strategy.gate_classical_s2 import ResearchPlan
from app.tests.test_gate_s2c_confluence import make_bars
from scripts.gate_s3_economic_diagnostic import (
    encode, read_snapshot, write_snapshot, SYMBOLS, FRAMES,
)

STRATEGY = "EMA_TREND_PULLBACK_V1"


def trade(index, net):
    return replay.Trade(STRATEGY, "LONG", index, index+1, index+2,
                        100, 100+net, 1, 98, 104, net, 0, 0, net, "TARGET")


def report(trades):
    net = sum(t.net_pnl for t in trades)
    return replay.ReplayReport(STRATEGY, "TEST", 100, tuple(trades),
                               1000, 1000+net, net, net/10, None, None, 0, 0, 0)


def test_bridge_retains_occupancy_and_equity_effects_instead_of_adding_vetoes():
    baseline = report([trade(1, -2), trade(5, 5), trade(10, -3)])
    filtered = report([trade(1, -1), trade(8, -4)])
    result = economics.pnl_bridge(baseline, filtered)
    assert result["common_decision_net_difference"] == 1
    assert result["baseline_only_net_pnl"] == 2
    assert result["filtered_only_net_pnl"] == -4
    assert result["filtered_minus_baseline_net_pnl"] == -5
    assert result["marginal_filter_effect_identified"] is False
    assert economics.pnl_bridge(report([]), report([]))["filtered_minus_baseline_net_pnl"] == 0


def test_bridge_rejects_duplicate_decisions_and_unreconciled_pnl():
    with pytest.raises(ValueError, match="Duplicate"):
        economics.pnl_bridge(report([trade(1, 2), trade(1, 3)]), report([]))
    with pytest.raises(ValueError, match="reconcile"):
        economics.pnl_bridge(replace(report([trade(1, 2)]), net_pnl=999), report([]))
    with pytest.raises(ValueError, match="Incomparable"):
        economics.pnl_bridge(report([]), replace(report([]), start_equity=2000))


def groups():
    return {tf: make_bars(tf, n=160, descending=(tf == "4h")) for tf in FRAMES}


def signal(strategy, prefix, *, as_of, historical_replay):
    assert historical_replay and prefix[-1].closed_at < as_of
    chosen = len(prefix) % 5 == 1
    return ResearchPlan(strategy, "1.0.0", "BTCUSDT", "15m", as_of,
                        "LONG" if chosen else "WAIT", 1 if chosen else None,
                        2 if chosen else None, "SYNTHETIC_TEST", False, False)


def test_baseline_veto_outcomes_reconcile_and_do_not_change_frozen_replay(monkeypatch):
    monkeypatch.setattr(replay, "evaluate_strategy", signal)
    monkeypatch.setattr(confluence, "evaluate_strategy", signal)
    monkeypatch.setattr(economics, "evaluate_strategy", signal)
    data = groups()
    before = encode({tf: [b.model_dump(mode="json") for b in rows]
                     for tf, rows in data.items()})
    result = economics.diagnose_pair(data, STRATEGY, start_index=130)
    summary = result["summary"]
    assert summary["baseline"]["trades"] > 0
    assert summary["filtered"]["trades"] == 0
    outcomes = summary["baseline_trade_outcomes_by_filter_verdict"]
    assert outcomes["VETOED"]["trades"] == summary["baseline"]["trades"]
    assert outcomes["VETOED"]["net_pnl"] == pytest.approx(summary["baseline"]["net_pnl"])
    assert outcomes["ALLOWED"]["trades"] == 0
    costs = summary["baseline"]["cost_attribution"]
    assert costs["raw_gross_before_slippage"]-costs["slippage_drag"]-(
        costs["exchange_fees"]+costs["funding_estimate"]
    ) == pytest.approx(summary["baseline"]["net_pnl"], abs=4e-6)
    frozen = replay.replay_segment(STRATEGY, data["15m"], start_index=130,
                                  end_index=160, segment="REUSED_HISTORY_EXPLORATORY")
    assert [x["trade"] for x in result["baseline_trade_trace"]] == [
        asdict(t) for t in frozen.trades]
    assert all(x["verdict"]["filtered_action"] == "WAIT"
               for x in result["baseline_trade_trace"])
    assert summary["profitability_proven"] is False
    assert summary["execution_authorized"] is False
    assert summary["policy_mutation_authorized"] is False
    assert encode(result) == encode(economics.diagnose_pair(data, STRATEGY, start_index=130))
    assert before == encode({tf: [b.model_dump(mode="json") for b in rows]
                             for tf, rows in data.items()})


def test_veto_at_baseline_decision_does_not_see_later_higher_candles():
    data = groups()
    i = 130
    cutoff = data["15m"][i].closed_at+timedelta(microseconds=1)
    left = economics.TracedConfluence({tf: data[tf] for tf in ("1h", "4h")})
    # Inspect the actual frozen chart, including confirmed pivots and regimes.
    known = left._time_locked_chart(data["15m"][:i+1], cutoff)
    changed = {tf: [b.model_copy(update={"high": b.high+10000})
                    if b.closed_at > cutoff else b for b in data[tf]]
               for tf in ("1h", "4h")}
    right = economics.TracedConfluence(changed)
    assert known == right._time_locked_chart(data["15m"][:i+1], cutoff)


@pytest.mark.parametrize("change", ["gap", "symbol", "receipt", "venue"])
def test_snapshot_provenance_cannot_be_silently_mixed(change):
    data = groups()
    update = {"gap": {"closed_at": data["1h"][5].closed_at+timedelta(minutes=1)},
              "symbol": {"symbol": "ETHUSDT"},
              "receipt": {"received_at": data["1h"][5].closed_at-timedelta(seconds=1)},
              "venue": {"exchange": Exchange.BINANCE}}
    data["1h"][5] = data["1h"][5].model_copy(update=update[change])
    with pytest.raises(ValueError, match="provenance"):
        economics.diagnose_pair(data, STRATEGY, start_index=130)


def test_snapshot_checksum_rejects_changes_before_replay(tmp_path):
    snap = {"schema": "gate-economic-snapshot-v1",
            "source": "https://api-testnet.gateapi.io",
            "series": {s: {} for s in SYMBOLS}}
    path = tmp_path/"snapshot.json.gz"
    digest = write_snapshot(snap, path)
    assert read_snapshot(path) == (snap, digest)
    path.write_bytes(path.read_bytes()+b"changed")
    with pytest.raises(ValueError, match="checksum"):
        read_snapshot(path)


def test_snapshot_source_must_remain_testnet(tmp_path):
    path = tmp_path/"snapshot.json.gz"
    write_snapshot({"schema": "gate-economic-snapshot-v1", "source": "different",
                    "series": {s: {} for s in SYMBOLS}}, path)
    with pytest.raises(ValueError, match="source"):
        read_snapshot(path)
