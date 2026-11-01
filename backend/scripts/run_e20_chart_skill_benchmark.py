"""Offline E2.0 benchmark with public, frozen historical SPOT candles.

This program requires only local CSV. No network, credentials, database, OMS,
Railway or broker. A result is NOT agent promotion or real-time validation.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.strategy.research_benchmark_e20 import (
    DATASETS, RESEARCH_PROTOCOL, benchmark_series, protocol_hash,
)


def run(fixtures: Path) -> dict:
    rows = []
    for symbol, timeframe in DATASETS:
        name = f"e20_{symbol}_{timeframe}_binance_spot_20261031.csv"
        rows.append(benchmark_series(fixtures / name,
                                     symbol=symbol, timeframe=timeframe))
    return {
        "research": RESEARCH_PROTOCOL,
        "research_protocol_sha256": protocol_hash(),
        "benchmarks": rows,
        "execution_authorized": False,
        "live_testnet_modified": False,
        "deployed_agent_improved": "NOT_TESTED",
        "profitability_consistency_proven": False,
    }


def main() -> int:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "market_samples"
    result = run(fixtures)
    print("E2_BENCHMARK_RESULT " + json.dumps(
        result, sort_keys=True, separators=(",", ":"), allow_nan=False
    ))
    print("E2_BENCHMARK_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
