"""Local research evidence store. It never sends orders or computes profit."""
import json
import math
import re
import sqlite3
from datetime import datetime, timezone


class InvalidResearch(ValueError):
    pass


class ResearchJournal:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS research (
            revision TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
            candle TEXT NOT NULL, price REAL NOT NULL, action TEXT NOT NULL,
            agents TEXT NOT NULL, PRIMARY KEY (revision, symbol, timeframe, candle))""")
        self.db.commit()

    def close(self):
        self.db.close()

    def record(self, result, revision):
        try:
            if not isinstance(result, dict) or result.get('exchange') != 'GATE' or result.get('market') != 'GATE_USDT_FUTURES_TESTNET':
                raise ValueError()
            if result.get('analysis_scope') != 'RESEARCH_ONLY' or result.get('execution_enabled') is not False or result.get('source') != 'AGENTS':
                raise ValueError()
            if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{7,64}', revision):
                raise ValueError()
            symbol, timeframe = result['symbol'], result['timeframe']
            if symbol not in {'BTCUSDT', 'ETHUSDT', 'SOLUSDT'} or timeframe not in {'1m', '5m', '15m', '1h', '4h'}:
                raise ValueError()
            dt = datetime.fromisoformat(result['candle_closed_at'].replace('Z', '+00:00'))
            if dt.tzinfo is None:
                raise ValueError()
            candle = dt.astimezone(timezone.utc).isoformat()
            if isinstance(result['reference_price'], bool):
                raise ValueError()
            price = float(result['reference_price'])
            if not math.isfinite(price) or price <= 0 or result.get('reference_price_type') != 'LAST_CLOSED_CANDLE':
                raise ValueError()
            action = result['decision']['candidate_action']
            if action not in {'BUY', 'SELL', 'WAIT', 'HOLD'}:
                raise ValueError()
            outputs = result['agents']
            if not isinstance(outputs, list) or not outputs or len(outputs) != result['total_agents'] or len(outputs) > 1000:
                raise ValueError()
            agents = []
            seen = set()
            for row in outputs:
                name, status, signal, confidence = (row[k] for k in ('agent_name', 'status', 'signal', 'confidence'))
                if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9._-]{0,127}', name) or name in seen:
                    raise ValueError()
                if not isinstance(status, str) or not re.fullmatch(r'[A-Z_]{1,32}', status) or not isinstance(signal, str) or not re.fullmatch(r'[A-Z_]{1,32}', signal):
                    raise ValueError()
                if type(confidence) is not int or not 0 <= confidence <= 100:
                    raise ValueError()
                seen.add(name)
                agents.append({'name': name, 'status': status, 'signal': signal, 'confidence': confidence})
            agents_json = json.dumps(sorted(agents, key=lambda x: x['name']), separators=(',', ':'))
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            raise InvalidResearch('invalid research snapshot') from None
        with self.db:
            key = (revision, symbol, timeframe, candle)
            previous = self.db.execute('SELECT price, action, agents FROM research WHERE revision=? AND symbol=? AND timeframe=? AND candle=?', key).fetchone()
            if previous:
                if previous != (price, action, agents_json):
                    raise InvalidResearch('conflicting snapshot for candle and revision')
                return False
            self.db.execute('INSERT INTO research VALUES (?, ?, ?, ?, ?, ?, ?)', key + (price, action, agents_json))
        return True
