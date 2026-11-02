"""Offline diagnosis now; separately score post-freeze history without refitting.

No market downloads, orders, deployed services or durable S3 journal are used.
Post-freeze batch replay still assumes hypothetical bar availability and fills.
"""
import argparse
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path

from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import STRATEGIES
from app.strategy.gate_entry_exit_research import HYPOTHESES, historical_case, holdout_case
from scripts.gate_cost_viability_experiment import source_hashes as old_source_hashes, save_result
from scripts.gate_s3_economic_diagnostic import read_snapshot, encode, SYMBOLS, FRAMES

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT/"docs/research/gate-s3i-20261102"
SNAPSHOT = ROOT/"docs/research/gate-s3g-20261102/snapshot.json.gz"


def source_hashes():
    hashes = old_source_hashes()
    for p in ("app/strategy/gate_entry_exit_research.py", "scripts/gate_entry_exit_experiment.py"):
        hashes[p] = hashlib.sha256((ROOT/"backend"/p).read_bytes()).hexdigest()
    return hashes


def protocol():
    data = (DIR/"protocol.json").read_bytes()
    p = json.loads(data)
    required = {
        "id": "GATE_ENTRY_EXIT_RESEARCH_V1", "hypotheses": list(HYPOTHESES),
        "confirmation_bars": 1,
        "confirmation": "FAVORABLE_CLOSE_AND_NO_PRIOR_STOP_DISTANCE_EXCURSION",
        "target": "MAX_ORIGINAL_TARGET_OR_2_STOP_PLUS_3_ESTIMATED_ROUND_TRIP_COST",
        "stops_risk_and_maximum_holding_unchanged": True,
        "base_friction": {"fee_bps_side": 6, "slippage_bps_side": 6, "funding_bps_8h": 1},
        "stress_friction": {"fee_bps_side": 6, "slippage_bps_side": 12, "funding_bps_8h": 2},
        "minimum_training_completed_trades": 5, "minimum_holdout_bars": 96,
        "minimum_holdout_completed_trades_each_arm": 30,
        "stress_policy_retuned": False, "automatic_promotion_authorized": False,
        "execution_authorized": False, "historical_window_is_pristine": False,
        "post_freeze_replay_uses_hypothetical_receipts": True,
        "training_selection": "COMPLETED_NET_BEATS_CASH_BASELINE_AND_S1_WHERE_AVAILABLE_THEN_MAX_NET_ID_TIE",
        "holdout_selection": "FROZEN_FULL_OLD_SNAPSHOT_SELECTION_ONLY",
    }
    if any(p.get(k) != v for k, v in required.items()):
        raise ValueError("Protocol changed from the implemented hypotheses")
    start = datetime.fromisoformat(p["holdout_start_utc"])
    declared = datetime.fromisoformat(p["declared_at_utc"])
    if start.utcoffset() is None or declared.utcoffset() is None or start <= declared:
        raise ValueError("Invalid freeze clocks")
    return p, hashlib.sha256(data).hexdigest()


def groups_from(snapshot, symbol):
    groups = {tf: tuple(Candle.model_validate(r) for r in rows)
              for tf, rows in snapshot["series"][symbol].items()}
    if set(groups) != set(FRAMES) or any(
            b.symbol != symbol or b.timeframe != tf for tf, rows in groups.items() for b in rows):
        raise ValueError("Mixed market provenance")
    return groups


def historical():
    p, p_hash = protocol()
    snap, s_hash = read_snapshot(SNAPSHOT)
    if s_hash != p["historical_snapshot_sha256"]:
        raise ValueError("Wrong historical training snapshot")
    cases = []
    for symbol in SYMBOLS:
        groups = groups_from(snap, symbol)
        if any(len(rows) != 800 for rows in groups.values()):
            raise ValueError("Incomplete frozen historical data")
        for tf in FRAMES:
            for strategy in sorted(STRATEGIES):
                case = historical_case(groups, strategy, tf)
                cases.append(case)
                print("ENTRY_EXIT_CASE "+encode({
                    "symbol": symbol, "tf": tf, "strategy": strategy,
                    "future_selected": case["holdout_selected"],
                    "fixed_completed_net": {v: x["metrics"]["completed_trade_net"]
                        for v, x in case["fixed_reused_history"]["base"].items()},
                }).decode(), flush=True)
    folds = [f for c in cases for f in c["chronological_folds"]]
    return {"protocol": p, "protocol_sha256": p_hash, "source_sha256": source_hashes(),
            "snapshot_sha256": s_hash, "cases": cases,
            "summary": {"cases": len(cases), "chronological_folds": len(folds),
                "folds_selected": sum(f["selected"] is not None for f in folds),
                "folds_sufficient": sum(f["evidence"]["sufficient_completed_trades_each_arm"] for f in folds),
                "folds_descriptive_positive": sum(f["evidence"]["descriptive_screen_positive"] for f in folds),
                "frozen_holdout_candidates": sum(c["holdout_selected"] is not None for c in cases)},
            "historical_is_pristine": False, "post_freeze_evaluation": "PENDING_NOT_SUPPLIED",
            "accounts_are_independent_not_portfolio": True,
            "profitability_proven": False, "execution_authorized": False}


def frozen_training():
    p, digest = protocol()
    freeze = json.loads((DIR/"freeze.json").read_text())
    if (freeze["protocol_sha256"] != digest or freeze["source_sha256"] != source_hashes()
            or freeze["holdout_start_utc"] != p["holdout_start_utc"]):
        raise ValueError("Frozen protocol or source changed; holdout reuse forbidden")
    data = (DIR/"results.json.gz").read_bytes()
    if hashlib.sha256(data).hexdigest() != freeze["historical_result_sha256"]:
        raise ValueError("Frozen training result changed")
    result = json.loads(gzip.decompress(data))
    if result["protocol_sha256"] != digest or result["source_sha256"] != source_hashes():
        raise ValueError("Training/source mismatch")
    return p, result


def post_freeze(snapshot_path):
    p, old = frozen_training()
    snap, digest = read_snapshot(snapshot_path)
    start = datetime.fromisoformat(p["holdout_start_utc"])
    captured = datetime.fromisoformat(snap["capture_completed_at"])
    if captured.utcoffset() is None:
        raise ValueError("Naive capture timestamp")
    if captured < start:
        return {"status": "WAITING_FOR_POST_FREEZE_DATA", "not_before_utc": p["holdout_start_utc"],
                "profitability_proven": False, "execution_authorized": False}
    all_groups = {s: groups_from(snap, s) for s in SYMBOLS}
    if any(b.closed_at > captured or b.received_at > captured
           for groups in all_groups.values() for rows in groups.values() for b in rows):
        raise ValueError("Candle/receipt later than snapshot capture")
    cases = [{"symbol": c["symbol"], "timeframe": c["timeframe"], "strategy": c["strategy"],
              "result": holdout_case(all_groups[c["symbol"]], c, start)} for c in old["cases"]]
    return {"status": "POST_FREEZE_BATCH_REPLAY_ONLY", "snapshot_sha256": digest,
            "protocol_sha256": old["protocol_sha256"], "cases": cases,
            "selection_refit_on_holdout": False, "observed_execution": False,
            "profitability_proven": False, "execution_authorized": False}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--future-snapshot", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    result = post_freeze(args.future_snapshot) if args.future_snapshot else historical()
    if args.verify and gzip.decompress(args.verify.read_bytes()) != encode(result)+b"\n":
        raise ValueError("Evidence differs from archived diagnostic")
    save_result(result, args.output)
    print("ENTRY_EXIT_DONE "+encode(result.get("summary", {"status": result.get("status")})).decode())


if __name__ == "__main__":
    main()
