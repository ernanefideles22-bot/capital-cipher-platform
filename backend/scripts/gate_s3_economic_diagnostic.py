"""Capture public GET Testnet history or replay a SHA-verified immutable snapshot.

Only this CLI imports networking. No current shadow/journal/service is opened.
New captures are exploratory and cannot replace a frozen prospective window.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import STRATEGIES
from app.strategy.gate_s3_economic_diagnostic import diagnose_pair

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
FRAMES = ("15m", "1h", "4h")


def encode(value):
    def default(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(type(obj).__name__)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=default).encode()


async def capture():
    from app.demo.gate_market import fetch_candles
    snapshot = {"schema": "gate-economic-snapshot-v1",
                "source": "https://api-testnet.gateapi.io",
                "market": "USDT perpetual futures Testnet",
                "normalized_candles_not_raw_http": True,
                "capture_started_at": datetime.now(timezone.utc).isoformat(),
                "series": {}}
    for symbol in SYMBOLS:
        snapshot["series"][symbol] = {}
        for tf in FRAMES:
            rows = await fetch_candles(symbol, tf, limit=800)
            received = datetime.now(timezone.utc)
            candles = [Candle(
                exchange=Exchange.GATE, symbol=symbol, timeframe=tf,
                open=r["open"], high=r["high"], low=r["low"], close=r["close"],
                volume=r["volume"], closed_at=datetime.fromtimestamp(
                    r["close_time_ms"]/1000, timezone.utc), received_at=received,
            ).model_dump(mode="json") for r in rows]
            snapshot["series"][symbol][tf] = candles
    snapshot["capture_completed_at"] = datetime.now(timezone.utc).isoformat()
    return snapshot


def write_snapshot(snapshot, path):
    data = gzip.compress(encode(snapshot), mtime=0)
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    path.with_suffix(path.suffix+".sha256").write_text(digest+"\n")
    return digest


def read_snapshot(path):
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if path.with_suffix(path.suffix+".sha256").read_text().strip() != digest:
        raise ValueError("Snapshot checksum mismatch")
    snap = json.loads(gzip.decompress(data))
    if (snap.get("schema") != "gate-economic-snapshot-v1"
            or snap.get("source") != "https://api-testnet.gateapi.io"
            or set(snap.get("series", {})) != set(SYMBOLS)):
        raise ValueError("Unexpected snapshot source or scope")
    return snap, digest


def analyze(snapshot, digest):
    results = []
    for symbol in SYMBOLS:
        groups = {tf: tuple(Candle.model_validate(x) for x in rows)
                  for tf, rows in snapshot["series"][symbol].items()}
        if any(x.symbol != symbol for rows in groups.values() for x in rows):
            raise ValueError("Mislabeled snapshot symbol")
        for strategy in sorted(STRATEGIES):
            result = diagnose_pair(groups, strategy)
            results.append(result)
            print("GATE_S3_ECONOMIC_PAIR "+encode(result["summary"]).decode(), flush=True)
    source_root = Path(__file__).resolve().parents[1]
    sources = ["app/strategy/gate_classical_s2.py", "app/strategy/gate_s2_replay.py",
               "app/strategy/gate_s2c_confluence.py",
               "app/strategy/professional_gate_chart_stage1.py",
               "app/strategy/gate_s2_loss_diagnostics.py",
               "app/strategy/gate_s3_economic_diagnostic.py",
               "scripts/gate_s3_economic_diagnostic.py"]
    return {"snapshot_sha256": digest,
            "source_sha256": {p: hashlib.sha256((source_root/p).read_bytes()).hexdigest()
                              for p in sources},
            "snapshot_source": snapshot["source"],
            "capture_completed_at": snapshot["capture_completed_at"],
            "research_accounts_are_separate_not_portfolio": True,
            "normalized_snapshot_contains_actual_batch_receipts": True,
            "replay_uses_hypothetical_historical_availability": True,
            "testnet_is_not_production_liquidity_or_execution": True,
            "profitability_proven": False, "execution_authorized": False,
            "results": results}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    if args.capture == bool(args.snapshot):
        parser.error("Choose exactly one of --capture or --snapshot")
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.snapshot or args.output/"snapshot.json.gz"
    if args.capture:
        if path.exists():
            raise ValueError("Refusing to overwrite archived snapshot")
        write_snapshot(asyncio.run(capture()), path)
    snapshot, digest = read_snapshot(path)
    result = analyze(snapshot, digest)
    (args.output/"economic-diagnostic.json").write_bytes(encode(result)+b"\n")
    print("GATE_S3_ECONOMIC_DONE "+encode({
        "snapshot_sha256": digest, "comparisons": len(result["results"]),
        "profitability_proven": False, "execution_authorized": False,
    }).decode(), flush=True)


if __name__ == "__main__":
    main()
