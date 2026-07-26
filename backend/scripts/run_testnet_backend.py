"""Start the Bybit TESTNET API only after the isolated preflight passes."""

from __future__ import annotations

import json
import os

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
                "event": "BYBIT_TESTNET_RUNTIME_AUTHORIZED",
                "report": report.model_dump(),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--workers",
            "1",
        ],
    )


if __name__ == "__main__":
    main()
