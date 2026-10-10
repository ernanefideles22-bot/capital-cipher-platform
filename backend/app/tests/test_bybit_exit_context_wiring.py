"""Composition-root safety for the Bybit TESTNET emergency exit service."""

from pathlib import Path


def test_bybit_exit_service_is_internal_and_has_no_public_route():
    root = Path(__file__).resolve().parents[1]
    context = (root / 'api' / 'context.py').read_text(encoding='utf-8')
    routes = root / 'api' / 'routes'
    route_text = '\n'.join(
        item.read_text(encoding='utf-8') for item in routes.glob('*.py')
    )

    assert 'BybitTestnetExitService' in context
    assert 'bybit_testnet_exit_service=bybit_testnet_exit_service' in context
    assert 'flatten_after_kill_switch' not in route_text
    assert 'submit_reduce_only_exit' not in route_text
