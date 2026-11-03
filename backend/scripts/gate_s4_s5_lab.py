"""On-demand, local-only S4/S5 CLI; no scheduled task or hosted integration."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from app.strategy.gate_s4_lab import Hypothesis, register_campaign, run_campaign
from app.strategy.gate_s5_selection import selection_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "run", "report"))
    parser.add_argument("--lab", required=True, help="Separate local campaign directory")
    parser.add_argument("--dataset", help="Canonical GATE Candle JSON array")
    parser.add_argument("--proposals", help="Optional declarative hypothesis JSON array")
    parser.add_argument("--budget", type=int, default=6)
    args = parser.parse_args()
    if args.command in {"register", "run"} and not args.dataset:
        parser.error("--dataset is required for register/run")
    if args.command != "register" and args.proposals:
        parser.error("Proposals must be frozen during registration")
    try:
        if args.command == "register":
            proposals = None
            if args.proposals:
                p = Path(args.proposals)
                if p.stat().st_size > 32768:
                    raise ValueError("Proposal byte budget exceeded")
                proposals = tuple(Hypothesis(**v) for v in json.loads(p.read_text()))
            result = register_campaign(args.lab, args.dataset, proposals, budget=args.budget)
        elif args.command == "run":
            run_campaign(args.lab, args.dataset)
            result = selection_report(args.lab)
        else:
            result = selection_report(args.lab)
    except (ValueError, OSError, TypeError, KeyError, sqlite3.Error) as exc:
        parser.exit(2, f"S4/S5 laboratory blocked: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
