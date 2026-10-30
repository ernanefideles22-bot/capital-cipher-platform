"""Export private measured canary evidence from the existing volume, read-only."""

import argparse
import json
import sqlite3
from pathlib import Path

from app.execution.gate_demo_release import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha256", required=True)
    parser.add_argument(
        "--journal", default="/gate-data/capital-cipher-gate-testnet/cycles.sqlite3"
    )
    args = parser.parse_args()
    path = Path(args.journal).resolve()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT payload FROM gate_demo_canary_evidence WHERE sha256=?",
            (args.sha256,),
        ).fetchone()
    if not row or digest(json.loads(row[0])) != args.sha256:
        raise SystemExit("Matching immutable Gate demo evidence not found")
    print(
        json.dumps(
            {"sha256": args.sha256, "evidence": json.loads(row[0])},
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
