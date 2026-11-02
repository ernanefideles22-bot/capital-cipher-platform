"""Explicit offline export; never initialize or modify the source journal."""
import argparse
import json
from datetime import datetime
from pathlib import Path

from app.strategy.gate_s3_durable_research import S3JournalBlocked
from app.strategy.gate_s3_financial_review import financial_review, render_markdown


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.journal.resolve().parent):
        parser.error("Output must be outside the source journal directory")
    try:
        report = financial_review(args.journal, as_of=args.as_of)
    except S3JournalBlocked as exc:
        parser.exit(2, f"EVIDENCE_UNAVAILABLE: {exc}; no PnL inferred\n")
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)+"\n"
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/"financial-review.json").write_text(encoded)
    (args.output/"financial-review.md").write_text(render_markdown(report))


if __name__ == "__main__":
    main()
