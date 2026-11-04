"""Register/run/review the fixed historical economic reference, without orders."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from app.strategy import gate_scenario_financial_replay as f
from app.strategy import gate_scenario_observer as o
from app.strategy import gate_scenario_journal as parent


def fingerprint():
    return o.digest({'replay':hashlib.sha256(Path(f.__file__).read_bytes()).hexdigest(),
        'driver':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'parent':parent.fingerprint(),
        'risk_exit_helpers':hashlib.sha256(Path(f._exit_trigger.__code__.co_filename).read_bytes()).hexdigest(),
        'source_helpers':hashlib.sha256(Path(f.source.__file__).read_bytes()).hexdigest()})


def database(root):
    path=Path(root).absolute()/'study/financial-measurement.sqlite3'
    if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('Journal symlink refused')
    return path


def connect(root,readonly=False):
    db=sqlite3.connect(database(root).as_uri()+('?mode=ro' if readonly else '?mode=rw'),uri=True)
    if readonly:db.execute('PRAGMA query_only=ON')
    return db


def append(db,kind,value):
    last=db.execute('SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1').fetchone()
    previous=last[0] if last else '0'*64
    payload=o.canonical(value)
    db.execute('INSERT INTO events(kind,payload,previous_hash,event_hash) VALUES(?,?,?,?)',
        (kind,payload,previous,o.digest([previous,kind,payload])))


def events(db):
    out=[];head='0'*64
    for seq,kind,payload,previous,sha in db.execute('SELECT * FROM events ORDER BY seq'):
        if seq!=len(out)+1 or previous!=head or sha!=o.digest([previous,kind,payload]):raise ValueError('Journal chain mismatch')
        out.append((kind,json.loads(payload)));head=sha
    phases=[k for k,_ in out]
    if (phases not in [['REGISTERED'],['REGISTERED','STARTED'],['REGISTERED','STARTED','FAILED'],['REGISTERED','STARTED','RESULT']]
            or out[0][1]['fingerprint']!=fingerprint() or out[0][1]['rules']!=json.loads(o.canonical(f.rules()))):
        raise ValueError('Frozen rules/source or phase mismatch')
    return out,head


def register(root,old_root):
    data,source=f.load_sources(old_root,root)
    path=database(root);path.parent.mkdir(exist_ok=True)
    with path.open('xb'):pass
    value={'registered_at_utc':datetime.now(timezone.utc).isoformat(),'fingerprint':fingerprint(),
        'rules':f.rules(),'source':source,'data_sha256':o.digest(data),
        'sources_collected_before_final_code_freeze':True,'source_intents_declared_before_downloads':True,
        'post_hoc_derived_hypothesis':True,'globally_unseen_history_certified':False}
    with closing(connect(root)) as db,db:
        db.executescript("""CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TRIGGER no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;""")
        append(db,'REGISTERED',value)
    return review(root)


def review(root):
    with closing(connect(root,True)) as db:values,head=events(db)
    return {'journal_head':head,'phases':[k for k,_ in values],'registration':values[0][1],
        'result':next((v for k,v in values if k=='RESULT'),None),
        'failure':next((v for k,v in values if k=='FAILED'),None),'profitability_proven':False,'execution_authorized':False}


def run(root,old_root):
    with closing(connect(root)) as db,db:
        db.execute('BEGIN IMMEDIATE');values,_=events(db)
        if len(values)!=1:raise ValueError('Study already started; no retry/reset')
        append(db,'STARTED',{'at_utc':datetime.now(timezone.utc).isoformat()})
    try:
        data,sources=f.load_sources(old_root,root)
        if o.digest(data)!=values[0][1]['data_sha256'] or sources!=values[0][1]['source']:raise ValueError('Registered data changed')
        context=f.Context(data);cases=[]
        for block in (0,1):
            for scenario in f.SCENARIOS:
                value=f.replay(data,context,block,scenario);cases.append(value)
                print(o.canonical({k:value[k] for k in ('block','scenario','closed_trades','net_closed_point','net_closed_lower','screen_passed')}),flush=True)
        result={'cases':cases,'all_case_screens_passed':all(v['screen_passed'] for v in cases),
            'distinct_calendar_blocks':2,'correlated_case_references':6,'parameter_search':0,
            'new_financial_case_replays':6,'significance_tests':0,'source':sources,
            'profitability_proven':False,'eligible_for_s5':False,'execution_authorized':False}
    except Exception as error:
        with closing(connect(root)) as db,db:append(db,'FAILED',{'error':str(error),'type':type(error).__name__})
        raise
    else:
        with closing(connect(root)) as db,db:append(db,'RESULT',result)
    return review(root)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('register','run','review'));p.add_argument('--root',required=True)
    p.add_argument('--old-root');p.add_argument('--output',required=True)
    args=p.parse_args()
    with Path(args.output).open('x') as out:
        result=review(args.root) if args.command=='review' else globals()[args.command](args.root,args.old_root)
        out.write(o.canonical(result)+'\n')


if __name__=='__main__':main()
