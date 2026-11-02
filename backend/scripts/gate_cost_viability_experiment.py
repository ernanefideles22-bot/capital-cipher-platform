"""Offline fixed cost hypotheses on previously observed checksum-pinned data.

No market downloads, service imports, journal, execution or promotion. The
entire archived snapshot was already observed: chronological diagnostics are
not a virgin holdout, even when a fold's selection ignores its later returns.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import gzip
import hashlib
import json
from pathlib import Path

from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import STRATEGIES
from app.strategy.gate_cost_viability_research import (
    BASE_FRICTION, STRESS_FRICTION, experiment_case,
)
from scripts.gate_s3_economic_diagnostic import read_snapshot, encode, SYMBOLS, FRAMES

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT = ROOT/"docs/research/gate-s3g-20261102/snapshot.json.gz"
DEFAULT_PROTOCOL = ROOT/"docs/research/gate-s3h-20261102/protocol.json"


def validate_protocol(protocol):
    required = {
        "protocol_id": "GATE_COST_VIABILITY_RESEARCH_V1",
        "snapshot_sha256": "02c406b670f8e802c2b48df1d1275f92b1a0683d8566d455e21ca4aa7be96eb5",
        "symbols": list(SYMBOLS), "timeframes": list(FRAMES),
        "base_friction": asdict(BASE_FRICTION), "stress_friction": asdict(STRESS_FRICTION),
        "target_to_cost_multiples": [2, 4],
        "gate_funding_horizon": "FROZEN_STRATEGY_MAX_HOLD",
        "gate_price": "LAST_KNOWN_CANDLE_CLOSE",
        "stress_gate_estimate": "UNCHANGED_BASE_ASSUMPTIONS_NO_RETUNING",
        "initial_train_bars": 400, "forward_bars": 120,
        "minimum_train_completed_trades": 5,
        "minimum_forward_completed_trades_each_arm": 30,
        "selection": "MAX_COMPLETED_TRADE_NET_BEATING_CASH_BASELINE_AND_S1_WHERE_AVAILABLE",
        "selection_tie_breaker": "VARIANT_ID_ASCENDING",
        "selection_ignores_boundary_mark_pnl": True,
        "fixed_diagnostic_start_fraction": .6, "context_filter_available_for": ["15m"],
        "independent_research_accounts_not_portfolio": True,
        "all_snapshot_data_previously_observed": True, "pristine_holdout": False,
        "future_validation_required": True, "execution_authorized": False,
        "policy_promotion_authorized": False,
    }
    if any(protocol.get(k) != v for k, v in required.items()):
        raise ValueError("Protocol differs from the fixed implemented experiment")


def source_hashes():
    backend = ROOT/"backend"
    sources = [
        "app/strategy/gate_cost_viability_research.py", "scripts/gate_cost_viability_experiment.py",
        "app/strategy/gate_classical_s2.py", "app/strategy/gate_s2_replay.py",
        "app/strategy/gate_s2_loss_diagnostics.py", "app/strategy/gate_s2c_confluence.py",
        "app/strategy/professional_gate_chart_stage1.py", "app/strategy/market_intelligence.py",
        "app/agents/indicators.py", "app/schemas/market.py", "app/schemas/common.py",
        "app/schemas/events.py", "scripts/gate_s3_economic_diagnostic.py",
    ]
    return {p: hashlib.sha256((backend/p).read_bytes()).hexdigest() for p in sources}


def run_experiment(snapshot_path, protocol_path):
    protocol_bytes = protocol_path.read_bytes()
    protocol = json.loads(protocol_bytes)
    validate_protocol(protocol)
    snapshot, digest = read_snapshot(snapshot_path)
    if digest != protocol["snapshot_sha256"]:
        raise ValueError("Wrong snapshot for this predeclared diagnostic")
    cases = []
    for symbol in SYMBOLS:
        groups = {tf: tuple(Candle.model_validate(row) for row in rows)
                  for tf, rows in snapshot["series"][symbol].items()}
        if (set(groups) != set(FRAMES) or any(
                len(rows) != 800 or any(b.symbol != symbol or b.timeframe != tf for b in rows)
                for tf, rows in groups.items())):
            raise ValueError("Incomplete or mixed research snapshot")
        for tf in FRAMES:
            for strategy in sorted(STRATEGIES):
                case = experiment_case(groups, strategy, tf)
                cases.append(case)
                fixed = case["fixed_arms"]["base"]
                folds = case["walk_forward_folds"]
                print("GATE_COST_CASE "+encode({
                    "symbol": symbol, "timeframe": tf, "strategy": strategy,
                    "fixed_completed_net": {v: r["metrics"]["completed_trade_net"]
                                            for v, r in fixed.items()},
                    "fixed_completed_trades": {v: r["metrics"]["completed_trades"]
                                               for v, r in fixed.items()},
                    "selected_forward_folds": sum(f["selected_variant"] is not None for f in folds),
                    "sufficient_forward_folds": sum(f["sufficient_completed_trades_each_arm"] for f in folds),
                    "descriptive_screen_positive_folds": sum(f["descriptive_screen_positive"] for f in folds),
                }).decode(), flush=True)
    all_folds = [f for c in cases for f in c["walk_forward_folds"]]
    return {
        "protocol": protocol, "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
        "snapshot_sha256": digest, "snapshot_source": snapshot["source"],
        "source_sha256": source_hashes(), "cases": cases,
        "summary": {
            "market_strategy_timeframe_cases": len(cases),
            "walk_forward_folds_separate_accounts": len(all_folds),
            "selected_forward_folds": sum(f["selected_variant"] is not None for f in all_folds),
            "sufficient_forward_folds": sum(f["sufficient_completed_trades_each_arm"] for f in all_folds),
            "descriptive_screen_positive_folds": sum(f["descriptive_screen_positive"] for f in all_folds),
        },
        "pristine_holdout": False, "future_validation_required": True,
        "profitability_proven": False, "execution_authorized": False,
        "policy_promotion_authorized": False,
    }


def save_result(result, output):
    data = gzip.compress(encode(result)+b"\n", mtime=0)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    output.with_suffix(output.suffix+".sha256").write_text(hashlib.sha256(data).hexdigest()+"\n")


def main():
    p = argparse.ArgumentParser(__doc__)
    p.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--verify", type=Path,
                   help="Compare decompressed saved evidence, including source hashes")
    args = p.parse_args()
    result = run_experiment(args.snapshot, args.protocol)
    if args.verify and gzip.decompress(args.verify.read_bytes()) != encode(result)+b"\n":
        raise ValueError("Archived evidence does not match this replay")
    save_result(result, args.output)
    print("GATE_COST_DONE "+encode(result["summary"]).decode(), flush=True)


if __name__ == "__main__":
    main()
