"""Validate staging PAPER environment variables without network access."""

from __future__ import annotations

import json

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
                "event": "STAGING_PAPER_PREFLIGHT_PASSED",
                "report": report.model_dump(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
