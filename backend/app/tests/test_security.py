"""Security boundaries: PAPER/TESTNET backend and isolated virtual-funds DEMO."""

from __future__ import annotations

import ast
import importlib.util
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


def _order_boundary_violations(relative_path: pathlib.Path, content: str) -> list[str]:
    # Negative network fixtures intentionally mention prohibited URLs. Only the
    # actual tests subtree is excluded, never production files named test_*.
    if relative_path.parts[0] == "tests":
        return []
    forbidden_fragments = [
        "api.binance.com/api/v3/order",
        "api-testnet.bybit.com/v5/order/create",
        "api.bybit.com/v5/order/create",
    ]
    violations = [fragment for fragment in forbidden_fragments if fragment in content]
    if relative_path.parts[0] == "demo" and any(
        origin in content for origin in ("https://fapi.binance.com", "http://fapi.binance.com")
    ):
        violations.append("Live Futures origin inside isolated DEMO")
    # Relative paths do not identify the venue. Only these existing isolated
    # DEMO modules may use the Futures order path; their transport is tested
    # against a literal demo-only origin, including redirects and hostile URLs.
    if "/fapi/v1/order" in content and relative_path.as_posix() not in {
        "demo/execution.py", "demo/venue.py",
    }:
        violations.append("Futures order path outside isolated DEMO transport/executor")

    if relative_path.parts[0] != "demo":
        package = ".".join(("app", *relative_path.parent.parts))
        for node in ast.walk(ast.parse(content)):
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = importlib.util.resolve_name("." * node.level + module, package)
                imports = [module, *(f"{module}.{alias.name}" for alias in node.names)]
            if any(name == "app.demo" or name.startswith("app.demo.") for name in imports):
                violations.append("Isolated DEMO imported by primary backend")
    return violations


def test_no_real_order_code_in_backend():
    """Keep live URL prohibitions and enforce the separate DEMO boundary."""
    for py_file in APP_DIR.rglob("*.py"):
        violations = _order_boundary_violations(py_file.relative_to(APP_DIR), py_file.read_text())
        assert not violations, f"{py_file}: {violations}"


@pytest.mark.parametrize("path,source", [
    ("api/routes/orders.py", 'url = "https://api.binance.com/api/v3/order"'),
    ("demo/venue.py", 'url = "https://api.bybit.com/v5/order/create"'),
    ("demo/execution.py", 'url = "https://api-testnet.bybit.com/v5/order/create"'),
    ("demo/venue.py", 'url = "https://api.binance.com/api/v3/order"'),
    ("demo/execution.py", 'origin = "https://fapi.binance.com"'),
    ("oms/service.py", 'path = "/fapi/v1/order"'),
    ("demo/other.py", 'path = "/fapi/v1/order"'),
    ("core/test_disguised.py", 'path = "/fapi/v1/order"'),
    ("main.py", "from app.demo.venue import FuturesDemoVenue"),
    ("api/context.py", "import app.demo.main"),
    ("main.py", "from app import demo"),
    ("api/context.py", "from ..demo import venue"),
    ("main.py", "from . import demo"),
])
def test_order_boundary_guard_rejects_production_and_cross_app_regressions(path, source):
    assert _order_boundary_violations(pathlib.Path(path), source)


@pytest.mark.parametrize("path", ["demo/venue.py", "demo/execution.py", "tests/test_negative_network.py"])
def test_order_boundary_guard_recognizes_isolated_demo_and_negative_fixtures(path):
    assert not _order_boundary_violations(pathlib.Path(path), 'path = "/fapi/v1/order"')


def test_no_hardcoded_secrets():
    """No obvious secrets committed in source (docs/16)."""
    import re

    pattern = re.compile(r"(api[_-]?key|secret|password)\s*=\s*[\"'][A-Za-z0-9+/]{20,}[\"']", re.I)
    for py_file in APP_DIR.rglob("*.py"):
        assert not pattern.search(py_file.read_text()), f"possible secret in {py_file}"


def test_logs_sanitize_sensitive_keys():
    import logging

    from app.core.logging import _sanitize, configure_logging

    clean = _sanitize(
        {
            "api_key": "abc",
            "token": "xyz",
            "other": 1,
            "nested": {"signature": "signed-query", "safe": True},
        }
    )
    assert clean["api_key"] == "***"
    assert clean["token"] == "***"
    assert clean["nested"]["signature"] == "***"
    assert clean["other"] == 1
    configure_logging("INFO")
    assert logging.getLogger("httpx").level == logging.WARNING
