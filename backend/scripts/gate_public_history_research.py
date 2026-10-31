"""Read-only Gate public futures OHLCV/funding research snapshot.

No authentication, no exchange order endpoints, no database updates.
Run in an isolated GitHub Action; never call this in the trading backend.
The retrospective REST data are *not* a point-in-time bid/ask history.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.candidate_replay import replay_candidate
from app.strategy.research_lab import StrategyHypothesis

API = "https://api.gateio.ws/api/v4"
INTERVAL_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400}
TARGETS = (("BTC_USDT", "15m"), ("ETH_USDT", "1h"), ("SOL_USDT", "4h"))
ROWS = 900
SOURCE = "Gate public USDT perpetual futures REST market candlesticks"
DEFAULT_CUTOFF = "2026-10-31T18:00:00+00:00"


def iso(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("UTC cutoff must have timezone")
    return dt.astimezone(timezone.utc).isoformat()


def sha(data: object) -> str:
    return hashlib.sha256(json.dumps(
        data, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def public_get(path: str, params: dict[str, object]) -> object:
    """Fixed host/path; caller supplies no URL. Public GET only."""
    if path not in ("/futures/usdt/candlesticks", "/futures/usdt/funding_rate"):
        raise ValueError("Endpoint not in read-only research allowlist")
    contract = params.get("contract")
    if not isinstance(contract, str) or not re.fullmatch(
        r"(?:BTC|ETH|SOL)_USDT", contract,
    ):
        raise ValueError("Unapproved contract")
    url = API + path + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url, headers={"Accept": "application/json",
                      "User-Agent": "CapitalCipher-ReadOnly-HistoricalResearch/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"Gate public endpoint returned {response.status}")
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError("Public API response too large")
    result = json.loads(raw)
    if not isinstance(result, list):
        raise ValueError("Public Gate endpoint returned a non-list payload")
    return result


def normalize_candles(
    contract: str, interval: str, raw: object, *,
    cutoff: datetime,
) -> list[Candle]:
    if (contract, interval) not in TARGETS or not isinstance(raw, list):
        raise ValueError("Unexpected contract, interval or API response")
    end = int(cutoff.timestamp())
    step = INTERVAL_SECONDS[interval]
    by_time: dict[int, Candle] = {}
    for record in raw:
        if not isinstance(record, dict) or not {"t", "o", "h", "l", "c", "v"} <= set(record):
            raise ValueError("Invalid Gate OHLCV candle payload")
        if not isinstance(record["t"], (int, float)) or record["t"] != int(record["t"]):
            raise ValueError("Non-integral Gate open timestamp")
        t = int(record["t"])
        if t % step:
            raise ValueError("Misaligned historical candle")
        if t + step > end:
            continue  # incomplete candle cannot be evaluated
        if t in by_time:
            raise ValueError("Duplicate historical candle")
        prices = [float(record[k]) for k in ("o", "h", "l", "c", "v")]
        if not all(math.isfinite(value) for value in prices):
            raise ValueError("Non-finite Gate OHLCV values")
        candle_close = datetime.fromtimestamp(t + step, tz=timezone.utc)
        # Exchange historical Kline does not provide initial receipt time.
        # Assume +1sec after close for deterministic RESEARCH ONLY.
        candle = Candle(
            exchange=Exchange.GATE,
            symbol=contract.replace("_", ""), timeframe=interval,
            open=prices[0], high=prices[1], low=prices[2],
            close=prices[3], volume=prices[4],
            closed_at=candle_close - timedelta(milliseconds=1),
            received_at=candle_close + timedelta(seconds=1),
        )
        by_time[t] = candle
    ordered = [by_time[k] for k in sorted(by_time)]
    if len(ordered) < 600:
        raise ValueError(f"Insufficient complete Gate candles: {len(ordered)}")
    if len(ordered) > 2000:
        raise ValueError("More than supported Gate candles in one response")
    for prev, following in zip(ordered, ordered[1:]):
        delta = following.closed_at - prev.closed_at
        if delta.total_seconds() != step:
            raise ValueError("Missing or nonconsecutive Gate candles")
    return ordered


def normalize_funding(raw: object, cutoff: datetime) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("Gate funding response is not a list")
    out = []
    seen = set()
    for record in raw:
        if not isinstance(record, dict) or not {"t", "r"} <= set(record):
            raise ValueError("Invalid Gate funding record")
        when = int(record["t"])
        rate = float(record["r"])
        if not math.isfinite(rate) or abs(rate) > .1:
            raise ValueError("Implausible funding rate")
        if when <= int(cutoff.timestamp()):
            if when in seen:
                raise ValueError("Duplicate funding timestamp")
            seen.add(when)
            out.append({"settlement_unix_s": when, "rate": rate})
    return sorted(out, key=lambda x: x["settlement_unix_s"])


def hypothesis(symbol: str, interval: str, side: str) -> StrategyHypothesis:
    return StrategyHypothesis(
        author_agent="GatePublicHistoryResearch",
        name=f"Gate historical trend momentum {symbol} {interval} {side}",
        family="MOMENTUM_REGIME",
        symbol=symbol, timeframe=interval, side=side,
        entry_features=(
            ("trend_up", "momentum_positive") if side == "BUY"
            else ("trend_down", "momentum_negative")
        ),
        invalidation_feature=("trend_down" if side == "BUY" else "trend_up"),
        parameters=(("atr_stop_multiple", 1.5), ("target_r_multiple", 2.0)),
        thesis="Evaluate a preregistered no-future-price momentum hypothesis "
               "with conservative next-available-bar execution and costs.",
    )


def summarize(contract: str, interval: str, candles: list[Candle]) -> dict:
    split = int(len(candles) * 0.6)
    symbol = contract.replace("_", "")
    experiments = []
    for side in ("BUY", "SELL"):
        p = hypothesis(symbol, interval, side)
        base = replay_candidate(p, candles[:split], evaluation_start=60,
                                all_in_cost_bps_per_side=12)
        later = replay_candidate(p, candles, evaluation_start=split,
                                 all_in_cost_bps_per_side=12)
        stress = replay_candidate(p, candles, evaluation_start=split,
                                  all_in_cost_bps_per_side=25)
        experiments.append({
            "fingerprint": p.fingerprint, "direction": side,
            "training_trades": len(base.trades),
            "training_net_usdt": base.net_pnl,
            "later_trades": len(later.trades),
            "later_net_usdt_12bps": later.net_pnl,
            "later_net_usdt_25bps": stress.net_pnl,
            "later_max_drawdown_pct": later.max_drawdown_percent,
            "execution_authorized": False,
        })
    return {
        "symbol": symbol, "timeframe": interval,
        "rows": len(candles),
        "first_closed_at": iso(candles[0].closed_at),
        "last_closed_at": iso(candles[-1].closed_at),
        "later_sample_from": iso(candles[split].closed_at),
        "candle_identity_sha256": sha([x.model_dump(mode="json") for x in candles]),
        "experiments": experiments,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
    iso(cutoff)
    if cutoff > datetime.now(timezone.utc):
        raise ValueError("Research cutoff is still in the future")
    destination = Path(args.out)
    destination.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "RESEARCH_ONLY_NOT_GATE_EXECUTION_PROOF",
        "source": SOURCE, "api_host": API,
        "snapshot_at": iso(datetime.now(timezone.utc)),
        "cutoff": iso(cutoff),
        "receipt_timestamps": "ASSUMED_CLOSE_PLUS_ONE_SECOND_NOT_OBSERVED",
        "note": "No historical bid/ask; funding observed retrospectively, not used in PnL",
        "pairs": [],
        "execution_authorized": False,
    }
    for contract, interval in TARGETS:
        seconds = INTERVAL_SECONDS[interval]
        finish = int(cutoff.timestamp())
        start = finish - (ROWS + 1) * seconds
        kline_raw = public_get(
            "/futures/usdt/candlesticks",
            {"contract": contract, "interval": interval,
             "from": start, "to": finish},
        )
        candles = normalize_candles(contract, interval, kline_raw, cutoff=cutoff)
        funding_raw = public_get(
            "/futures/usdt/funding_rate",
            {"contract": contract, "from": start, "to": finish,
             "limit": 1000},
        )
        funding = normalize_funding(funding_raw, cutoff)
        key = f"{contract}_{interval}".lower()
        sample = summarize(contract, interval, candles)
        sample["funding_rows"] = len(funding)
        sample["funding_limit_requested"] = 1000
        sample["funding_coverage_first_settlement"] = (
            datetime.fromtimestamp(funding[0]["settlement_unix_s"], timezone.utc).isoformat()
            if funding else None
        )
        sample["funding_coverage_last_settlement"] = (
            datetime.fromtimestamp(funding[-1]["settlement_unix_s"], timezone.utc).isoformat()
            if funding else None
        )
        sample["historical_funding_used_in_pnl"] = False
        sample["raw_kline_sha256"] = sha(kline_raw)
        sample["raw_funding_sha256"] = sha(funding_raw)
        report["pairs"].append(sample)
        (destination / f"{key}.json").write_text(json.dumps({
            "contract": contract, "interval": interval,
            "source": SOURCE,
            "cutoff": iso(cutoff),
            "capture_time": iso(datetime.now(timezone.utc)),
            "raw_candles": kline_raw,
            "raw_funding": funding_raw,
            "normalized_funding": funding,
            "receipt_timestamps": "ASSUMED_CLOSE_PLUS_ONE_SECOND_NOT_OBSERVED",
        }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print("GATE_PUBLIC_RESEARCH " + json.dumps(sample, sort_keys=True), flush=True)
        time.sleep(0.2)
    (destination / "research_manifest.json").write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    print("RESEARCH_COMPLETE " + json.dumps({
        "pairs": len(report["pairs"]), "cutoff": report["cutoff"],
        "execution_authorized": False,
        "venue_historical_quotes_available": False,
    }), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
