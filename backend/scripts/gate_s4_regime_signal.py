"""Register/train/evaluate one pooled forecast with frozen controls and gaps."""
import argparse
import json
from pathlib import Path
from app.strategy import gate_s4_regime_signal_study as study


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=("register","run","review"));p.add_argument("--root",required=True)
    p.add_argument("--catalog");p.add_argument("--source-root");p.add_argument("--output",required=True)
    args=p.parse_args()
    if args.command=="register":
        if not args.catalog:p.error("Catalog required")
        result=study.register(args.root,args.catalog)
    elif args.command=="run":result=study.run(args.root,args.source_root)
    else:result=study.review(args.root)
    with Path(args.output).open("x") as f:json.dump(result,f,indent=2,allow_nan=False)


if __name__=="__main__":main()
