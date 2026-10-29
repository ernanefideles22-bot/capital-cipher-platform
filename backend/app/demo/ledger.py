"""Small durable journal for the isolated Demo boundary; no credentials stored."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from app.demo.binance import DemoError

TERMINAL = ('CLOSED', 'REJECTED')


def utc():
    return datetime.now(timezone.utc).isoformat()


class DemoLedger:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, payload TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_trade ON trades((1)) WHERE state NOT IN ('CLOSED','REJECTED');
            CREATE UNIQUE INDEX IF NOT EXISTS one_signal_once ON trades(json_extract(payload, '$.signal_key')) WHERE json_extract(payload, '$.signal_key') IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS one_risk_approval_once ON trades(json_extract(payload, '$.approval_id')) WHERE json_extract(payload, '$.approval_id') IS NOT NULL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, time TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL);
        ''')
        # Automation never resumes implicitly after a deployment/restart.
        self.flag('automatic', False)

    def flag(self, key, value=None):
        if value is not None:
            self.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,json.dumps(value)))
        row = self.db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def proposal(self, data):
        self.db.execute('INSERT OR IGNORE INTO proposals VALUES (?,?,?)', (data['id'],json.dumps(data,allow_nan=False),utc()))
        return self.get_proposal(data['id'])

    def get_proposal(self, identity):
        row=self.db.execute('SELECT payload FROM proposals WHERE id=?',(identity,)).fetchone()
        if not row:
            raise DemoError('DEMO_PROPOSAL_NOT_FOUND')
        return json.loads(row[0])

    def trade(self, identity):
        row=self.db.execute('SELECT payload FROM trades WHERE id=?',(identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def reserve(self, data):
        existing=self.trade(data['id'])
        if existing:
            return existing
        try:
            self.db.execute('INSERT INTO trades VALUES (?,?,?,?)',(data['id'],data['state'],json.dumps(data,allow_nan=False),utc()))
        except sqlite3.IntegrityError:
            raise DemoError('DEMO_POSITION_OR_UNRESOLVED_ORDER_EXISTS') from None
        self.event('TRADE_RESERVED', {'id':data['id'],'symbol':data['symbol'],'side':data['side']})
        return data

    def update(self, identity, state, **fields):
        data=self.trade(identity)
        if data is None:
            raise DemoError('DEMO_TRADE_NOT_FOUND')
        data.update(fields, state=state, updated_at=utc())
        self.db.execute('UPDATE trades SET state=?,payload=?,updated=? WHERE id=?',(state,json.dumps(data,allow_nan=False),utc(),identity))
        self.event(state, {'id':identity,**{k:v for k,v in fields.items() if k in {'error_code','reason'}}})
        return data

    def trades(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM trades ORDER BY updated DESC LIMIT 100')]

    def active(self):
        return [x for x in self.trades() if x['state'] not in TERMINAL]

    def event(self, kind, data):
        previous=self.db.execute('SELECT hash FROM events ORDER BY seq DESC LIMIT 1').fetchone()
        previous=previous[0] if previous else '0'*64
        stamp=utc(); payload=json.dumps(data,sort_keys=True,allow_nan=False)
        digest=hashlib.sha256((previous+stamp+kind+payload).encode()).hexdigest()
        self.db.execute('INSERT INTO events(time,kind,payload,previous,hash) VALUES (?,?,?,?,?)',(stamp,kind,payload,previous,digest))

    def events(self):
        return [dict(r) for r in self.db.execute('SELECT seq,time,kind,payload,hash FROM events ORDER BY seq DESC LIMIT 100')]

    def close(self):
        self.db.close()
