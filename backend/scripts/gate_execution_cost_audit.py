"""Register/run/review bounded public execution quotes, without account access."""
import argparse
import json
from pathlib import Path
from app.strategy import gate_execution_cost_audit as audit


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("register", "run", "review"))
    p.add_argument("--root", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    value = getattr(audit, a.command)(a.root)
    with Path(a.output).open("x") as f:
        json.dump(value, f, indent=2, allow_nan=False)


if __name__ == "__main__":
    main()
