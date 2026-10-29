"""Durable, fail-closed Gate Testnet order-cycle journal.

This journal never transmits an exchange request and is not an executor.  It
records *intent before dispatch* and consumes independently acquired evidence;
pending or ambiguous steps remain locked across restart.  A future executor
must prove ownership, risk approval, exclusivity and exchange reconciliation.
"""
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from app.demo.gate_stop_validation import stop_matches


class CycleBlocked(ValueError):
    pass


def _quantity(value):
    if isinstance(value, bool):
        raise CycleBlocked('invalid position')
    try:
        n=Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise CycleBlocked('invalid position') from None
    if not n.is_finite():
        raise CycleBlocked('invalid position')
    return n


class GateCycleJournal:
    def __init__(self, path):
        self.db=sqlite3.connect(path, timeout=5, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS cycles (id TEXT PRIMARY KEY, state TEXT NOT NULL, contract TEXT NOT NULL, target TEXT NOT NULL, planned TEXT NOT NULL, position TEXT, stop_id TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL, old_state TEXT, new_state TEXT NOT NULL, reason TEXT NOT NULL)')

    def close(self):
        self.db.close()

    def read(self, cycle_id):
        row=self.db.execute('SELECT id,state,contract,target,planned,position,stop_id FROM cycles WHERE id=?',(cycle_id,)).fetchone()
        if row is None:
            raise CycleBlocked('unknown cycle')
        return dict(zip(('id','state','contract','target','planned','position','stop_id'),row))

    def create(self, cycle_id, contract, target, signed_planned_size):
        if not isinstance(cycle_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',cycle_id):
            raise CycleBlocked('invalid cycle id')
        n, planned=_quantity(target),_quantity(signed_planned_size)
        if contract not in {'BTC_USDT','ETH_USDT','SOL_USDT'} or n <= 0 or planned == 0:
            raise CycleBlocked('invalid plan')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('INSERT INTO cycles (id,state,contract,target,planned) VALUES (?, ?, ?, ?, ?)',(cycle_id,'CREATED',contract,str(n),str(planned)))
            self.db.execute('INSERT INTO events (cycle_id,old_state,new_state,reason) VALUES (?,NULL,?,?)',(cycle_id,'CREATED','plan recorded'))
            self.db.execute('COMMIT')
        except sqlite3.IntegrityError:
            self.db.execute('ROLLBACK')
            raise CycleBlocked('cycle already exists') from None
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _transition(self, cycle_id, expected, new_state, reason, *, position=None, stop_id=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            current=self.read(cycle_id)
            if current['state'] not in expected:
                raise CycleBlocked('unexpected or unresolved state')
            self.db.execute('UPDATE cycles SET state=?,position=COALESCE(?,position),stop_id=COALESCE(?,stop_id) WHERE id=?',
                            (new_state,position,stop_id,cycle_id))
            self.db.execute('INSERT INTO events (cycle_id,old_state,new_state,reason) VALUES (?,?,?,?)',
                            (cycle_id,current['state'],new_state,reason))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def record_entry_intent(self, cycle_id):
        self._transition(cycle_id, {'CREATED'}, 'ENTRY_PENDING', 'entry intent')

    def record_entry_confirmation(self, cycle_id, *, signed_position, owned_order_confirmed):
        # A timeout, partial evidence, wrong ownership, or no position must halt;
        # a pending/unknown write is never submitted a second time by this class.
        position=_quantity(signed_position)
        row=self.read(cycle_id)
        planned=_quantity(row['planned'])
        if owned_order_confirmed is not True or position == 0 or position * planned <= 0 or abs(position) > abs(planned):
            self._transition(cycle_id, {'ENTRY_PENDING'}, 'HALTED', 'entry not reconciled')
            return
        self._transition(cycle_id, {'ENTRY_PENDING'}, 'STOP_REQUIRED', 'entry reconciled', position=str(position))

    def record_stop_intent(self, cycle_id):
        self._transition(cycle_id, {'STOP_REQUIRED'}, 'STOP_SUBMITTING', 'stop intent')

    def record_stop_acknowledgement(self, cycle_id, *, stop_id):
        if not isinstance(stop_id,str) or not stop_id.isdecimal() or int(stop_id) <= 0:
            raise CycleBlocked('invalid stop id')
        self._transition(cycle_id, {'STOP_SUBMITTING'}, 'STOP_PENDING', 'stop confirmation pending', stop_id=stop_id)

    def record_stop_confirmation(self, cycle_id, *, snapshot, observed_at, now):
        row=self.read(cycle_id)
        if row['state'] != 'STOP_PENDING':
            raise CycleBlocked('unexpected or unresolved state')
        valid=stop_matches(snapshot,order_id=row['stop_id'],contract=row['contract'],
                           position_size=row['position'],trigger_price=row['target'],observed_at=observed_at,now=now)
        self._transition(cycle_id, {'STOP_PENDING'}, 'PROTECTED' if valid else 'HALTED',
                         'stop verified' if valid else 'stop not verified')
        return valid

    def halt_unknown(self, cycle_id):
        self._transition(cycle_id, {'ENTRY_PENDING','STOP_REQUIRED','STOP_SUBMITTING','STOP_PENDING','EXIT_PENDING'},
                         'HALTED', 'ambiguous exchange outcome')

    def record_exit_intent(self, cycle_id):
        self._transition(cycle_id, {'PROTECTED'}, 'EXIT_PENDING', 'exit intent')

    def record_flat_confirmation(self, cycle_id, *, position, open_orders, open_stop_orders,
                                 all_positions_flat, exchange_confirmed):
        flat = (_quantity(position) == 0 and type(open_orders) is int and open_orders == 0
                and type(open_stop_orders) is int and open_stop_orders == 0
                and all_positions_flat is True and exchange_confirmed is True)
        self._transition(cycle_id, {'EXIT_PENDING'}, 'CLOSED' if flat else 'HALTED',
                         'flat confirmed' if flat else 'flat not confirmed')
        return flat
