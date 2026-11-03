"""One-shot public Testnet history capture. No private endpoints or credentials.

Uses the environment's normal HTTPS proxy route. Existing S3 networking and
normalization remain unchanged. Raw pages and real batch receipt clocks survive.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from app.demo.gate_market import _CONTRACTS, _INTERVALS, _ORIGIN, normalize_candles
from app.schemas.market import Candle
from app.strategy.gate_s4_lab import _db_path, canonical, load_dataset


def _get(path, params=None):
    url = _ORIGIN + "/api/v4" + path
    if params:
        url += "?" + urlencode(params)
    requested = datetime.now(timezone.utc).isoformat()
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=20) as response:
        if response.status != 200 or urlparse(response.url).hostname != urlparse(_ORIGIN).hostname:
            raise ValueError("Public Testnet response origin/status changed")
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Public response byte budget exceeded")
        receipt = datetime.now(timezone.utc)
    return json.loads(raw), {"url": url, "requested_at_utc": requested,
                             "received_at_utc": receipt.isoformat(),
                             "response_bytes_sha256": hashlib.sha256(raw).hexdigest(),
                             "raw_response_utf8": raw.decode()}, receipt


def capture_series(output, symbol, timeframe, *, count=3000, anchor_seconds=None):
    if symbol not in _CONTRACTS or timeframe not in ("15m", "1h", "4h"):
        raise ValueError("Only registered GATE Testnet series")
    if type(count) is not int or not 600 <= count <= 4096:
        raise ValueError("Snapshot candle budget is 600..4096")
    _db_path(output)  # Reuse hosted-root and symlink isolation guard.
    directory = Path(output) / f"{symbol}-{timeframe}"
    directory.mkdir(parents=True, exist_ok=False)
    step = _INTERVALS[timeframe]
    anchor = int(time.time()) if anchor_seconds is None else anchor_seconds
    if type(anchor) is not int or anchor > time.time():
        raise ValueError("Snapshot anchor must not be in the future")
    last = anchor // step * step
    first = last - (count - 1) * step
    raw_pages, candles = [], []
    try:
        contract = _CONTRACTS[symbol]
        meta, provenance, _ = _get("/futures/usdt/contracts/" + contract)
        raw_pages.append(provenance)
        if not isinstance(meta, dict) or meta.get("name") != contract:
            raise ValueError("Public contract provenance mismatch")
        cursor = first
        while cursor <= last:
            size = min(500, (last - cursor) // step + 1)
            upper = cursor + (size - 1) * step
            values, provenance, receipt = _get("/futures/usdt/candlesticks", {
                "contract": contract, "interval": timeframe, "from": cursor - step, "to": upper - 1})
            raw_pages.append(provenance)
            normalized = normalize_candles(values, interval_seconds=step, now_seconds=upper,
                                            contract_multiplier=meta["quanto_multiplier"])
            if (len(normalized) != size or normalized[0]["close_time_ms"] != cursor * 1000
                    or normalized[-1]["close_time_ms"] != upper * 1000):
                raise ValueError("Missing candles or unexpected requested range")
            candles.extend(Candle(exchange="GATE", symbol=symbol, timeframe=timeframe,
                                  open=v["open"], high=v["high"], low=v["low"], close=v["close"],
                                  volume=v["volume"], received_at=receipt,
                                  closed_at=datetime.fromtimestamp(v["close_time_ms"] / 1000, timezone.utc))
                           .model_dump(mode="json") for v in normalized)
            cursor = upper + step
        path = directory / "candles.json"
        with path.open("x") as f:
            f.write(canonical(candles))
        bars, sha = load_dataset(path)
        result = {"status": "AVAILABLE", "symbol": symbol, "timeframe": timeframe,
                  "path": str(path.resolve()), "candles": len(bars), "sha256": sha,
                  "first_closed_at": bars[0].closed_at.isoformat(),
                  "last_closed_at": bars[-1].closed_at.isoformat(),
                  "zero_volume_candles": sum(b.volume == 0 for b in bars),
                  "distinct_closes": len({b.close for b in bars})}
    except Exception as exc:
        result = {"status": "UNAVAILABLE", "symbol": symbol, "timeframe": timeframe,
                  "error_type": type(exc).__name__, "reason": str(exc)[:300]}
    with (directory / "source.json").open("x") as f:
        json.dump({"venue": "GATE_FUTURES_TESTNET_PUBLIC", "anchor_seconds": anchor,
                   "historical_receipts_reconstructed": False,
                   "batch_history_not_prospective": True, "exchange_liquidity_proven": False,
                   "raw_pages": raw_pages, "result": result}, f, indent=2, allow_nan=False)
    return result
