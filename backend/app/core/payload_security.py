"""Fail-closed validation for event payloads."""

from __future__ import annotations

from typing import Any

SENSITIVE_FIELD_NAMES = {
    "api_key",
    "api_secret",
    "authorization",
    "access_token",
    "refresh_token",
    "password",
    "private_key",
    "secret",
    "token",
}


def sensitive_payload_path(value: Any, path: str = "payload") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            child_path = f"{path}.{key}"
            if normalized in SENSITIVE_FIELD_NAMES:
                return child_path
            found = sensitive_payload_path(child, child_path)
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = sensitive_payload_path(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def ensure_payload_has_no_secrets(payload: dict[str, Any]) -> None:
    sensitive_path = sensitive_payload_path(payload)
    if sensitive_path is not None:
        raise ValueError(f"Sensitive field is forbidden in event payload: {sensitive_path}")
