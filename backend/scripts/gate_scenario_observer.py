"""Inspect or durably observe a Gate scenario from supplied public snapshots.

No networking, scheduler, broker, API keys, agent activation or orders.
Snapshot format: {as_of, candles: {15m: [...], 1h: [...], 4h: [...]}, minute: Candle}.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from app.strategy import gate_scenario_journal as journal
from app.strategy import gate_scenario_observer as observer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("analyze", "register", "observe", "review"))
    parser.add_argument("--snapshot")
    parser.add_argument("--root")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    # Refuse accidental output overwrites before any journal mutation.
    with Path(args.output).open("x") as output:
        if args.command == "review":
            value = journal.review(args.root)
        else:
            path = Path(args.snapshot)
            if path.stat().st_size > journal.MAX_SNAPSHOT_BYTES:
                raise ValueError("Snapshot file too large")
            snapshot = json.loads(path.read_text(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
            if args.command == "analyze":
                reading, bar, _, _ = observer.analyze_snapshot(snapshot)
                plan, reason = observer.create_plan(reading, bar.close)
                value = {"technical_analysis": asdict(reading), "annotations": observer.annotations(reading),
                         "plan": plan, "reason": reason, "execution_authorized": False}
            else:
                value = getattr(journal, args.command)(args.root, snapshot)
        output.write(observer.canonical(value) + "\n")


if __name__ == "__main__":
    main()
