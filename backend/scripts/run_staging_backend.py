"""Fail closed before replacing this process with the staging API server."""

from __future__ import annotations

import json
import os

from app.operations.staging import (
    load_staging_settings,
    validate_staging_environment,
)


def main() -> None:
    settings = load_staging_settings()
    report = validate_staging_environment(settings)
    print(
        json.dumps(
            {
                "event": "STAGING_PAPER_RUNTIME_AUTHORIZED",
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
