"""Validate the isolated Bybit TESTNET environment without network access."""

from __future__ import annotations

import json

from app.operations.testnet import (
    load_testnet_settings,
    validate_testnet_environment,
)


def main() -> None:
    settings = load_testnet_settings()
    report = validate_testnet_environment(settings)
    print(
        json.dumps(
            {
                "event": "BYBIT_TESTNET_PREFLIGHT_PASSED",
                "report": report.model_dump(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
