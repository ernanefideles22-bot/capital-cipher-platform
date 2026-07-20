"""Stable logical identities for candles and ordered candle datasets."""

from __future__ import annotations

import hashlib
import json
from datetime import timezone
from typing import Any

from app.schemas.market import Candle


def canonical_candle_payload(candle: Candle) -> dict[str, Any]:
    """Return market facts only; ingestion time is deliberately excluded."""
    payload = candle.model_dump(mode="json")
    payload.pop("received_at", None)
    payload["closed_at"] = (
        candle.closed_at.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return payload


def candle_event_id(candle: Candle) -> str:
    encoded = json.dumps(
        canonical_candle_payload(candle),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def candle_dataset_hash(candles: list[Candle]) -> str:
    digest = hashlib.sha256()
    for candle in candles:
        digest.update(candle_event_id(candle).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
