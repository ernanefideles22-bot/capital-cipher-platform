"""Public production-market data only. No account, order or mode-changing API.

This is a separate research source: the Testnet/S3 adapters remain unchanged.
"""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.demo.gate_market import _CONTRACTS, _INTERVALS, normalize_candles
from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import _check_bars
from app.strategy.gate_s4_lab import _db_path, canonical

ORIGIN = "https://api.gateio.ws"
VENUE = "GATE_FUTURES_PRODUCTION_PUBLIC_READ_ONLY"
MAX_CANDLES = 6000
MAX_BYTES = 16 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_get(path, params=None):
    allowed = {"/futures/usdt/candlesticks"} | {
        "/futures/usdt/contracts/" + contract for contract in _CONTRACTS.values()}
    if path not in allowed:
        raise ValueError("Only whitelisted public market GET endpoints")
    url = ORIGIN + "/api/v4" + path + ("?" + urlencode(params) if params else "")
    requested = datetime.now(timezone.utc)
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with build_opener(_NoRedirect()).open(request, timeout=20) as response:
        if response.status != 200 or not response.url.startswith(ORIGIN + "/api/v4/"):
            raise ValueError("Unexpected production public source/status")
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Public response byte budget")
        received = datetime.now(timezone.utc)
    return json.loads(raw), {"url": url, "requested_at_utc": requested.isoformat(),
        "received_at_utc": received.isoformat(), "response_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_response_utf8": raw.decode()}, received


def load_real_dataset(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Research dataset byte budget exceeded")
    raw = path.read_bytes()
    values = json.loads(raw)
    required = {"exchange", "symbol", "timeframe", "open", "high", "low", "close", "volume",
                "closed_at", "received_at"}
    if (not isinstance(values, list) or not 3000 <= len(values) <= MAX_CANDLES
            or any(not isinstance(v, dict) or not required <= v.keys() for v in values)):
        raise ValueError("Explicit canonical market history and receipt clocks required")
    bars = tuple(Candle.model_validate(v) for v in values)
    _check_bars(bars, bars[-1].closed_at + timedelta(microseconds=1), replay=True)
    return bars, hashlib.sha256(raw).hexdigest()


def capture_real_series(root, symbol, timeframe, *, anchor, count=6000):
    if (symbol not in _CONTRACTS or timeframe not in ("15m", "1h", "4h")
            or type(count) is not int or not 3000 <= count <= MAX_CANDLES
            or type(anchor) is not int or anchor > time.time()):
        raise ValueError("Unsupported or future production research request")
    _db_path(root)
    folder = Path(root) / f"{symbol}-{timeframe}"
    folder.mkdir(parents=True, exist_ok=False)
    pages, values = [], []
    step = _INTERVALS[timeframe]
    last = anchor // step * step
    first = last - (count - 1) * step
    try:
        contract = _CONTRACTS[symbol]
        meta, page, _ = public_get("/futures/usdt/contracts/" + contract)
        pages.append(page)
        if not isinstance(meta, dict) or meta.get("name") != contract:
            raise ValueError("Production contract metadata mismatch")
        cursor = first
        while cursor <= last:
            size = min(500, (last - cursor) // step + 1)
            upper = cursor + (size - 1) * step
            raw, page, receipt = public_get("/futures/usdt/candlesticks", {
                "contract": contract, "interval": timeframe, "from": cursor - step, "to": upper - 1})
            pages.append(page)
            bars = normalize_candles(raw, interval_seconds=step, now_seconds=upper,
                                     contract_multiplier=meta["quanto_multiplier"])
            if (len(bars) != size or bars[0]["close_time_ms"] != cursor * 1000
                    or bars[-1]["close_time_ms"] != upper * 1000):
                raise ValueError("Production history gap/range mismatch")
            values.extend(Candle(exchange="GATE", symbol=symbol, timeframe=timeframe,
                open=b["open"], high=b["high"], low=b["low"], close=b["close"], volume=b["volume"],
                closed_at=datetime.fromtimestamp(b["close_time_ms"] / 1000, timezone.utc),
                received_at=receipt).model_dump(mode="json") for b in bars)
            cursor = upper + step
        target = folder / "candles.json"
        with target.open("x") as f:
            f.write(canonical(values))
        bars, sha = load_real_dataset(target)
        result = {"status": "AVAILABLE", "symbol": symbol, "timeframe": timeframe,
                  "relative_path": f"{symbol}-{timeframe}/candles.json", "sha256": sha,
                  "candles": len(bars), "first_closed_at": bars[0].closed_at.isoformat(),
                  "last_closed_at": bars[-1].closed_at.isoformat(),
                  "zero_volume_candles": sum(b.volume == 0 for b in bars)}
    except Exception as exc:
        result = {"status": "UNAVAILABLE", "symbol": symbol, "timeframe": timeframe,
                  "error_type": type(exc).__name__, "reason": str(exc)[:300]}
    with (folder / "source.json").open("x") as f:
        json.dump({"venue": VENUE, "anchor_seconds": anchor, "result": result, "raw_pages": pages,
                   "batch_history_not_prospective": True, "real_execution_proven": False,
                   "account_access_used": False, "order_submission_authorized": False}, f, indent=2)
    return result


def capture_all(root, *, anchor, count=6000):
    _db_path(root)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = [pool.submit(capture_real_series, root, symbol, tf, anchor=anchor, count=count)
                 for symbol in _CONTRACTS for tf in ("15m", "1h", "4h")]
        for future in as_completed(tasks):
            row = future.result()
            rows.append(row)
            print(canonical(row), flush=True)
    rows.sort(key=lambda r: (r["symbol"], r["timeframe"]))
    catalog = {"venue": VENUE, "anchor_seconds": anchor, "series": rows,
               "requested_series": 9, "execution_authorized": False}
    with (root / "catalog.json").open("x") as f:
        json.dump(catalog, f, indent=2)
    return catalog
