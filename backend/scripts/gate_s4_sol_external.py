"""Register before fetching a disjoint past period, then test one frozen SOL lead."""
import argparse
import json
from pathlib import Path
from app.strategy import gate_s4_sol_external_study as study


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=("register","run","review"));p.add_argument("--root",required=True)
    p.add_argument("--catalog");p.add_argument("--discovery-root");p.add_argument("--output",required=True)
    a=p.parse_args()
    if a.command=="register":
        if not a.catalog or not a.discovery_root:p.error("Selection catalog and discovery evidence required")
        result=study.register(a.root,a.catalog,a.discovery_root)
    elif a.command=="run":result=study.run(a.root)
    else:result=study.review(a.root)
    with Path(a.output).open("x") as f:json.dump(result,f,indent=2,allow_nan=False)


if __name__=="__main__":main()
