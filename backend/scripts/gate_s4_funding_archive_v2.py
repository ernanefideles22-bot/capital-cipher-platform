"""Archive-source attempt preserving the frozen BTC economics and failed V1."""
import argparse
import json
from pathlib import Path
from app.strategy import gate_s4_funding_archive_v2 as study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "run", "review"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--prior-root")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "register":
        if not args.prior_root:
            parser.error("Preserved terminal V1 source failure is required")
        result = study.register(args.root, args.prior_root)
    else:
        result = getattr(study, args.command)(args.root)
    with Path(args.output).open("x") as f:
        json.dump(result, f, indent=2, allow_nan=False)


if __name__ == "__main__":
    main()
