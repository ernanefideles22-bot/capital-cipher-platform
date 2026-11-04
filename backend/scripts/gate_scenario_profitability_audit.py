"""Read-only readiness audit; input collection intent/receipts must already exist."""
import argparse
import json
from pathlib import Path

from app.strategy.gate_scenario_profitability_audit import audit


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",required=True)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    value=audit(args.root)
    with Path(args.output).open("x") as f:
        json.dump(value,f,indent=2,allow_nan=False)
        f.write("\n")


if __name__ == "__main__":
    main()
