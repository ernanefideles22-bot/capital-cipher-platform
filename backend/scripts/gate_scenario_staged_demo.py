"""V2 durable engineering demonstration using invented prices, never market PnL."""
import argparse
from pathlib import Path
from app.strategy import gate_scenario_staged_journal as journal
from app.strategy import gate_scenario_staged_levels as observer
from scripts.gate_scenario_demo import synthetic_snapshots

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    result = {"synthetic_only":True,"financial_replays":0,"version":observer.VERSION,"cases":[]}
    for side in ("SHORT","LONG"):
        for case in ("confirmed","failed_recheck","invalidated"):
            folder = args.root / f"{side.lower()}-{case}"
            for index, snapshot in enumerate(synthetic_snapshots(case,side=side)):
                report = journal.register(folder,snapshot) if index == 0 else journal.observe(folder,snapshot)
                if report["status"] in observer.TERMINAL | {"BLOCKED"}:
                    break
            result["cases"].append({"side":side,"case":case,"result":report})
    (args.root/"synthetic-demo.json").write_text(observer.canonical(result)+"\n")
    print(observer.canonical({"synthetic_only":True,"cases":[
        {"side":v["side"],"case":v["case"],"status":v["result"]["status"],
         "new_candidates":v["result"]["new_candidate_count"]} for v in result["cases"]]}))


if __name__ == "__main__":
    main()
