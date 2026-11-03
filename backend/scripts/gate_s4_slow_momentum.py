"""Register one fixed momentum thesis and replay only known training dates."""
import argparse
import json
from pathlib import Path

from app.strategy import gate_s4_slow_momentum_study as study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "train", "review"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--catalog")
    parser.add_argument("--source-root")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "register":
        if not args.catalog:
            parser.error("Catalog required")
        result = study.register(args.root, args.catalog)
    else:
        if args.command == "train":
            study.run(args.root, args.source_root)
        result = study.review(args.root)
    with Path(args.output).open("x") as output:
        json.dump(result, output, indent=2, allow_nan=False)


if __name__ == "__main__":
    main()
