"""Canonical identities for immutable backtesting research artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def walk_forward_artifact_hash(report: BaseModel | dict[str, Any]) -> str:
    """Hash deterministic research facts, excluding operational metadata."""

    if isinstance(report, BaseModel):
        payload = report.model_dump(mode="json")
    else:
        payload = dict(report)
    payload.pop("artifact_hash", None)
    payload.pop("created_at", None)
    payload.pop("duration_ms", None)
    return canonical_sha256(payload)
