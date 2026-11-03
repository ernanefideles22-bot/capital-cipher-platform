"""Explicit, local-only production-public historical research; never order routing."""
import argparse
import json
import time
from pathlib import Path

from app.strategy import gate_s4_real_study as study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "diagnose", "register", "run", "report"))
    parser.add_argument("--catalog")
    parser.add_argument("--prior-root")
    parser.add_argument("--diagnosis")
    parser.add_argument("--root")
    parser.add_argument("--source-root")
    parser.add_argument("--output", required=True)
    parser.add_argument("--anchor", type=int)
    args = parser.parse_args()
    if args.command == "capture":
        from app.strategy.gate_s4_real_source import capture_all
        capture_all(args.output, anchor=args.anchor if args.anchor is not None else int(time.time()), count=6000)
        return
    if args.command == "diagnose":
        if not args.catalog or not args.prior_root:
            parser.error("diagnose requires catalog/prior-root")
        study.diagnose(args.catalog, args.prior_root, args.output)
        return
    if not args.root:
        parser.error("Study root required")
    if args.command == "register":
        if not args.catalog or not args.diagnosis:
            parser.error("register requires catalog/diagnosis")
        result = study.register(args.root, args.catalog, args.diagnosis)
    else:
        if args.command == "run":
            study.run(args.root, source_root=args.source_root)
        result = study.report(args.root)
    with Path(args.output).open("x") as f:
        json.dump(result, f, indent=2, allow_nan=False)
    print(json.dumps({"report": str(Path(args.output).resolve()), "execution_authorized": False}))


if __name__ == "__main__":
    main()
