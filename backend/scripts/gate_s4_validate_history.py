"""Register/run/review an isolated nine-series historical S4 validation suite."""
import argparse
import json
from pathlib import Path

from app.strategy.gate_s4_validation import register_suite, run_suite, suite_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "run", "report"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--catalog")
    parser.add_argument("--source-root", help="Relocate archived sources without changing frozen hashes")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.command == "register":
        if not args.catalog:
            parser.error("--catalog is required for registration")
        report = register_suite(args.root, args.catalog)
    else:
        if args.command == "run":
            run_suite(args.root, source_root=args.source_root)
        report = suite_report(args.root)
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
        with Path(args.output).open("x") as f:
            f.write(encoded + "\n")
        print(json.dumps({"report": str(Path(args.output).resolve()),
                          "execution_authorized": False}))
    else:
        print(encoded)


if __name__ == "__main__":
    main()
