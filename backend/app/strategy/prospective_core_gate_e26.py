"""E2.6 FROZEN future-market campaign contract, no network/OMS/trading.

This manifest and its candidate rule are committed BEFORE the future market
window. Evaluation uses only an independently collected chronological candle
receipt file; backfilled historical OHLCV without receipt evidence is REJECTED.
A self-reported receipt timestamp alone is NOT independent attestation.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.candidate_replay import BAR_SECONDS
from app.strategy.core_signal_gate_e25 import (
    EXPERIMENT as E25_EXPERIMENT, experiment_digest as e25_digest,
)

CAMPAIGN = {
    "campaign_id": "E26_FUTURE_BINANCE_SPOT_NATIVE_BUY_GATE_20261105",
    "research_protocol_version": "E26_FIXED_SPOT_CORE_SIGNAL_GATE_FORWARD_V1",
    "preregistered_baseline": "E25_THREE_CORE_AGENT_BUY_ONLY",
    "preregistered_candidate": "E25_FROZEN_UPTREND_EMA20_4BAR_MOMENTUM_REL_VOL_0_8",
    "e25_protocol_sha256": e25_digest(),
    "markets": [["BTCUSDT", "15m"], ["ETHUSDT", "1h"], ["SOLUSDT", "4h"]],
    "first_evaluation_candle_closed_at_utc": "2026-11-05T00:00:00+00:00",
    "evaluation_candle_end_exclusive_utc": "2026-12-05T00:00:00+00:00",
    "warmup_candles_per_market": 300,
    "max_forward_receipt_lag_bars": 1,
    "cost_bps_per_side": [12.0, 25.0, 40.0],
    "max_trade_risk_fraction": 0.005,
    "min_closed_trades_for_descriptive_report": 30,
    "min_closed_trades_for_proof_candidate": 100,
    "required_independent_walkforward_folds": 3,
    "matched_fill_model_id": "E21_NEXT_AVAILABLE_OPEN_STOP_FIRST_R2_V1",
    "exchange": "BINANCE",
    "position_policy": "SPOT_LONG_ONLY_NO_SHORTS",
    "data_source_requirement": "EACH_FORWARD_BAR_RECORDED_NEAR_REAL_TIME_WITH_SOURCE_EVIDENCE",
    "collection_job_enabled": False,
    "continuous_collection_confirmed": False,
    "prospective_per_agent_300_capture_confirmed": False,
    "execution_authorized": False,
    "automatic_promotion_authorized": False,
    "real_money_authorized": False,
}
MARKETS = tuple(tuple(x) for x in CAMPAIGN["markets"])
BPS = tuple(CAMPAIGN["cost_bps_per_side"])
HEADER = (
    "open_time_ms,open,high,low,close,volume,observed_at_utc,source"
)
ALLOWED_SOURCE = "BINANCE_PUBLIC_SPOT_KLINES"


def campaign_sha256():
    return hashlib.sha256(json.dumps(
        CAMPAIGN, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()).hexdigest()


def _datetime(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timezone-aware observed_at mandatory")
    return parsed.astimezone(timezone.utc)


def expected_evaluation_count(timeframe):
    step = BAR_SECONDS[timeframe]
    start = _datetime(CAMPAIGN["first_evaluation_candle_closed_at_utc"])
    end = _datetime(CAMPAIGN["evaluation_candle_end_exclusive_utc"])
    delta = int((end-start).total_seconds())
    if delta <= 0 or delta % step:
        raise ValueError("Future campaign must have exact complete timeframe bars")
    return delta // step


def load_causally_observed_candles(
    csv_file: Path, *, symbol: str, timeframe: str, now: datetime
):
    """Fail closed on insufficient, late, synthetic or future observations.

    Only evaluation bars require close-time receipt. Warmup can be backfilled
    but its provenance/last 300 candles must still be verified as SPOT.
    A timestamp written into CSV is not externally attested and cannot on
    its own authorize an OOS claim.
    """
    if (symbol, timeframe) not in MARKETS:
        raise ValueError("Unexpected market not in frozen campaign")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Aware wall clock required")
    now = now.astimezone(timezone.utc)
    start = _datetime(CAMPAIGN["first_evaluation_candle_closed_at_utc"])
    end = _datetime(CAMPAIGN["evaluation_candle_end_exclusive_utc"])
    step = timedelta(seconds=BAR_SECONDS[timeframe])
    if now < end + step:
        raise ValueError("Future campaign not ended; no premature result access")
    blob = csv_file.read_bytes()
    lines = blob.decode("utf-8").splitlines()
    if not lines or lines[0] != HEADER:
        raise ValueError("Observed-bar CSV needs explicit receipt and source")
    rows = list(csv.DictReader(lines))
    evaluation_count = expected_evaluation_count(timeframe)
    required = CAMPAIGN["warmup_candles_per_market"] + evaluation_count
    if len(rows) != required:
        raise ValueError("Expected exact warmup and forward bar counts")
    candles = []
    first_eval_idx = CAMPAIGN["warmup_candles_per_market"]
    prev_closed = None
    for i, raw in enumerate(rows):
        if raw["source"] != ALLOWED_SOURCE:
            raise ValueError("Unverified/synthetic candle source")
        try:
            opened = datetime.fromtimestamp(
                int(raw["open_time_ms"])/1000, tz=timezone.utc,
            )
            closed = opened + step
            observed = _datetime(raw["observed_at_utc"])
            prices = tuple(float(raw[k]) for k in (
                "open", "high", "low", "close", "volume"
            ))
        except (TypeError, OverflowError, KeyError) as ex:
            raise ValueError("Malformed prospective OHLCV event") from ex
        if not all(math.isfinite(x) for x in prices):
            raise ValueError("Nonfinite price/volume")
        o,h,l,c,v = prices
        if (not min(o,h,l,c)>0 or v<0 or
            h<max(o,l,c) or l>min(o,h,c)):
            raise ValueError("Invalid OHLCV range")
        if prev_closed is not None and closed-prev_closed != step:
            raise ValueError("Gap, overlap, reorder or duplicate market event")
        prev_closed = closed
        if observed < closed or observed > now:
            raise ValueError("Candle observed before close or after actual evaluation")
        if i >= first_eval_idx and observed > closed + step*CAMPAIGN[
            "max_forward_receipt_lag_bars"
        ]:
            raise ValueError("Late/backfilled candle cannot count as forward observation")
        if i == first_eval_idx and closed != start:
            raise ValueError("Prospective evaluation start offset changed")
        if i == required-1 and closed != end-step:
            raise ValueError("Prospective evaluation end offset changed")
        candles.append(Candle(
            exchange=Exchange.BINANCE,symbol=symbol,timeframe=timeframe,
            open=o,high=h,low=l,close=c,volume=v,
            closed_at=closed,received_at=observed,
        ))
    return candles,{
        "symbol":symbol,"timeframe":timeframe,
        "file_sha256":hashlib.sha256(blob).hexdigest(),
        "bars":len(candles),
        "evaluation_candles":evaluation_count,
        "first_evaluation_closed_at":candles[first_eval_idx].closed_at.isoformat(),
        "last_evaluation_closed_at":candles[-1].closed_at.isoformat(),
        "receipt_times_independently_attested":False,
        "market_quotes_and_execution_spreads_observed":False,
        "market_source_external_authentication_performed":False,
        "source_provenance":"CSV_DECLARED_UNVERIFIED",
    }
