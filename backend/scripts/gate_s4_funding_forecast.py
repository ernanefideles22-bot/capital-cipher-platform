"""Register/run/review one past-only funding forecast diagnostic."""
import argparse
import json
from pathlib import Path
from app.strategy import gate_s4_funding_forecast as study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "run", "review"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = getattr(study, args.command)(args.root)
    with Path(args.output).open("x") as f:
        json.dump(result, f, indent=2, allow_nan=False)


if __name__ == "__main__":
    main()
