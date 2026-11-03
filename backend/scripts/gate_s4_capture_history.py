"""Explicit one-shot capture of nine public Gate Testnet series; no scheduling."""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.strategy.gate_s4_lab import _db_path
from app.strategy.gate_s4_public_snapshot import capture_series


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--bars", type=int, default=3000)
    args = parser.parse_args()
    _db_path(args.output)
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=False)
    anchor = int(time.time())
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(capture_series, root, symbol, tf, count=args.bars, anchor_seconds=anchor)
                   for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT") for tf in ("15m", "1h", "4h")]
        for future in as_completed(futures):
            value = future.result()
            results.append(value)
            print(json.dumps(value), flush=True)
    results.sort(key=lambda v: (v["symbol"], v["timeframe"]))
    with (root / "catalog.json").open("x") as f:
        json.dump({"venue": "GATE_FUTURES_TESTNET_PUBLIC", "requested_series": 9,
                   "anchor_seconds": anchor, "series": results, "execution_authorized": False}, f, indent=2)


if __name__ == "__main__":
    main()
