"""Security tests (docs/22, docs/16): Phase 1 must make real execution impossible."""

from __future__ import annotations

import pathlib

import pytest

APP_DIR = pathlib.Path(__file__).resolve().parents[1]


def test_no_live_mode_boot_configuration():
    """SYSTEM_MODE=LIVE must be rejected at configuration level."""
    from app.core.config import Settings

    with pytest.raises(Exception):
        Settings(SYSTEM_MODE="LIVE")
    with pytest.raises(Exception):
        Settings(SYSTEM_MODE="LIVE_LOCKED")


def test_no_private_api_key_settings_exist():
    """Phase 1 config must not even have fields for exchange API keys."""
    from app.core.config import Settings

    field_names = set(Settings.model_fields.keys())
    forbidden = {"binance_api_key", "binance_api_secret", "bybit_api_key", "bybit_api_secret",
                 "exchange_api_key", "exchange_api_secret"}
    assert not (field_names & forbidden)


def test_required_broker_cannot_start_without_redis_url():
    from app.core.config import Settings

    with pytest.raises(Exception):
        Settings(EVENT_BROKER_REQUIRED=True)


def test_no_real_order_code_in_backend():
    """No module may reference private exchange order endpoints."""
    forbidden_fragments = [
        "api.binance.com/api/v3/order",
        "api-testnet.bybit.com/v5/order/create",
        "api.bybit.com/v5/order/create",
        "/fapi/v1/order",
    ]
    for py_file in APP_DIR.rglob("*.py"):
        if py_file.name == "test_security.py":
            continue
        content = py_file.read_text()
        for fragment in forbidden_fragments:
            assert fragment not in content, f"{py_file} references real order endpoint"


def test_no_hardcoded_secrets():
    """No obvious secrets committed in source (docs/16)."""
    import re

    pattern = re.compile(r"(api[_-]?key|secret|password)\s*=\s*[\"'][A-Za-z0-9+/]{20,}[\"']", re.I)
    for py_file in APP_DIR.rglob("*.py"):
        assert not pattern.search(py_file.read_text()), f"possible secret in {py_file}"


def test_logs_sanitize_sensitive_keys():
    from app.core.logging import _sanitize

    clean = _sanitize({"api_key": "abc", "token": "xyz", "other": 1})
    assert clean["api_key"] == "***"
    assert clean["token"] == "***"
    assert clean["other"] == 1
