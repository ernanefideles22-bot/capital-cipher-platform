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
from app.demo.gate_entry_evidence import confirmed_entry
from app.demo.gate_exit_evidence import confirmed_exit


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
        self.db.execute('CREATE TABLE IF NOT EXISTS entry_identity (cycle_id TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE, order_id TEXT UNIQUE)')
        self.db.execute('CREATE TABLE IF NOT EXISTS exit_identity (cycle_id TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE, order_id TEXT UNIQUE)')

    def close(self):
        self.db.close()

    def read(self, cycle_id):
        row=self.db.execute('SELECT id,state,contract,target,planned,position,stop_id FROM cycles WHERE id=?',(cycle_id,)).fetchone()
        if row is None:
            raise CycleBlocked('unknown cycle')
        return dict(zip(('id','state','contract','target','planned','position','stop_id'),row))

    def recovery_status(self):
        """Report unresolved work after restart without modifying it or granting permission."""
        rows=self.db.execute("SELECT id,state,contract FROM cycles WHERE state != 'CLOSED' ORDER BY id").fetchall()
        return {'recovery_required':bool(rows), 'execution_authorized':False,
                'cycles':[{'id':row[0], 'state':row[1], 'contract':row[2]} for row in rows]}

    def create(self, cycle_id, contract, target, signed_planned_size):
        if not isinstance(cycle_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',cycle_id):
            raise CycleBlocked('invalid cycle id')
        n, planned=_quantity(target),_quantity(signed_planned_size)
        if contract not in {'BTC_USDT','ETH_USDT','SOL_USDT'} or n <= 0 or planned == 0:
            raise CycleBlocked('invalid plan')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            # Serialize account access across journal connections. Any open,
            # halted or ambiguous cycle blocks a second entry, including after
            # restart. Only a confirmed fully closed cycle releases the slot.
            if self.db.execute("SELECT 1 FROM cycles WHERE state != 'CLOSED' LIMIT 1").fetchone():
                raise CycleBlocked('another cycle is unresolved')
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

    def record_bound_entry_intent(self, cycle_id, client_id):
        """Persist the client id and intent together before any dispatch."""
        if not isinstance(client_id,str) or not re.fullmatch(r't-[A-Za-z0-9_.-]{1,28}', client_id):
            raise CycleBlocked('invalid client id')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            current=self.read(cycle_id)
            if current['state'] != 'CREATED':
                raise CycleBlocked('unexpected cycle state')
            if self.db.execute('SELECT 1 FROM exit_identity WHERE client_id=?',(client_id,)).fetchone():
                raise CycleBlocked('entry identity conflicts with exit')
            self.db.execute('INSERT INTO entry_identity(cycle_id,client_id) VALUES (?,?)',(cycle_id,client_id))
            self.db.execute("UPDATE cycles SET state='ENTRY_PENDING' WHERE id=?",(cycle_id,))
            self.db.execute('INSERT INTO events(cycle_id,old_state,new_state,reason) VALUES (?,?,?,?)',
                            (cycle_id,'CREATED','ENTRY_PENDING','bound entry intent'))
            self.db.execute('COMMIT')
        except sqlite3.IntegrityError:
            self.db.execute('ROLLBACK')
            raise CycleBlocked('entry identity already used') from None
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def record_entry_acknowledgement(self, cycle_id, order_id):
        """Store provider id exactly once; an unknown acknowledgement is not retried."""
        if not isinstance(order_id,str) or not re.fullmatch(r'[1-9][0-9]*',order_id):
            raise CycleBlocked('invalid order id')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            current=self.read(cycle_id)
            if current['state'] != 'ENTRY_PENDING':
                raise CycleBlocked('unexpected cycle state')
            if self.db.execute('SELECT 1 FROM exit_identity WHERE order_id=?',(order_id,)).fetchone():
                raise CycleBlocked('entry order id conflicts with exit')
            changed=self.db.execute('UPDATE entry_identity SET order_id=? WHERE cycle_id=? AND order_id IS NULL',
                                    (order_id,cycle_id)).rowcount
            if changed != 1:
                raise CycleBlocked('identity missing or acknowledgement already recorded')
            self.db.execute('COMMIT')
        except sqlite3.IntegrityError:
            self.db.execute('ROLLBACK')
            raise CycleBlocked('provider id already used') from None
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def record_verified_entry(self, cycle_id, *, order, position, order_observed_at, position_observed_at, now):
        """Use bound identifiers and independently acquired snapshots; no write authorization."""
        row=self.read(cycle_id)
        if row['state'] != 'ENTRY_PENDING':
            raise CycleBlocked('unexpected cycle state')
        identity=self.db.execute('SELECT client_id,order_id FROM entry_identity WHERE cycle_id=?',(cycle_id,)).fetchone()
        if not identity or not identity[1]:
            raise CycleBlocked('entry acknowledgement missing')
        filled=confirmed_entry(order=order,position=position,contract=row['contract'],order_id=identity[1],
                               client_id=identity[0],planned_size=row['planned'],
                               order_observed_at=order_observed_at,position_observed_at=position_observed_at,now=now)
        if filled is None:
            self._transition(cycle_id, {'ENTRY_PENDING'}, 'HALTED', 'entry evidence not reconciled')
            return False
        self.record_entry_confirmation(cycle_id,signed_position=filled,owned_order_confirmed=True)
        return True

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

    def record_stop_confirmation(self, cycle_id, *, snapshot, observed_at, now, confirmed_position):
        row=self.read(cycle_id)
        if row['state'] != 'STOP_PENDING':
            raise CycleBlocked('unexpected or unresolved state')
        try:
            valid=_quantity(confirmed_position)==_quantity(row['position']) and stop_matches(snapshot,order_id=row['stop_id'],contract=row['contract'],
                               position_size=row['position'],trigger_price=row['target'],observed_at=observed_at,now=now)
        except CycleBlocked:
            # Missing or invalid position evidence cannot establish protection.
            valid=False
        self._transition(cycle_id, {'STOP_PENDING'}, 'PROTECTED' if valid else 'HALTED',
                         'stop verified' if valid else 'stop not verified')
        return valid

    def halt_unknown(self, cycle_id):
        self._transition(cycle_id, {'ENTRY_PENDING','STOP_REQUIRED','STOP_SUBMITTING','STOP_PENDING','EXIT_PENDING','EMERGENCY_EXIT_PENDING'},
                         'HALTED', 'ambiguous exchange outcome')

    def record_emergency_exit_intent(self, cycle_id, *, confirmed_position, owned_position_confirmed):
        """Record intent only after independent ownership and position evidence.

        This transition does not submit or authorize an exchange request.
        """
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.read(cycle_id)
            if row['state'] not in {'STOP_REQUIRED', 'STOP_PENDING', 'HALTED'} or not row['position']:
                raise CycleBlocked('no reconciled position for emergency exit')
            if row['state'] == 'HALTED':
                last=self.db.execute('SELECT old_state,reason FROM events WHERE cycle_id=? ORDER BY seq DESC LIMIT 1', (cycle_id,)).fetchone()
                if last != ('STOP_PENDING','stop not verified'):
                    raise CycleBlocked('halt requires independent reconciliation')
            if self.db.execute("SELECT 1 FROM events WHERE cycle_id=? AND new_state='EMERGENCY_EXIT_PENDING'", (cycle_id,)).fetchone():
                raise CycleBlocked('previous emergency exit outcome requires reconciliation')
            if owned_position_confirmed is not True or _quantity(confirmed_position) != _quantity(row['position']):
                raise CycleBlocked('emergency exit position not independently confirmed')
            self.db.execute("UPDATE cycles SET state='EMERGENCY_EXIT_PENDING' WHERE id=?", (cycle_id,))
            self.db.execute('INSERT INTO events (cycle_id,old_state,new_state,reason) VALUES (?,?,?,?)',
                            (cycle_id,row['state'],'EMERGENCY_EXIT_PENDING','emergency exit intent'))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def record_exit_intent(self, cycle_id):
        self._transition(cycle_id, {'PROTECTED'}, 'EXIT_PENDING', 'exit intent')

    def record_bound_exit_intent(self, cycle_id, client_id):
        """Persist an exit id with intent before any future dispatch."""
        if not isinstance(client_id,str) or not re.fullmatch(r't-[A-Za-z0-9_.-]{1,28}',client_id):
            raise CycleBlocked('invalid exit client id')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.read(cycle_id)
            if row['state'] != 'PROTECTED':
                raise CycleBlocked('position is not confirmed protected')
            if self.db.execute('SELECT 1 FROM entry_identity WHERE client_id=?',(client_id,)).fetchone():
                raise CycleBlocked('exit identity conflicts with entry')
            self.db.execute('INSERT INTO exit_identity(cycle_id,client_id) VALUES (?,?)',(cycle_id,client_id))
            self.db.execute("UPDATE cycles SET state='EXIT_PENDING' WHERE id=?",(cycle_id,))
            self.db.execute('INSERT INTO events(cycle_id,old_state,new_state,reason) VALUES (?,?,?,?)',
                            (cycle_id,'PROTECTED','EXIT_PENDING','bound exit intent'))
            self.db.execute('COMMIT')
        except sqlite3.IntegrityError:
            self.db.execute('ROLLBACK')
            raise CycleBlocked('exit identity already used') from None
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def record_exit_acknowledgement(self, cycle_id, order_id):
        if not isinstance(order_id,str) or not re.fullmatch(r'[1-9][0-9]*',order_id):
            raise CycleBlocked('invalid exit order id')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.read(cycle_id)['state'] != 'EXIT_PENDING':
                raise CycleBlocked('unexpected cycle state')
            if self.db.execute('SELECT 1 FROM entry_identity WHERE order_id=?',(order_id,)).fetchone():
                raise CycleBlocked('exit order id conflicts with entry')
            changed=self.db.execute('UPDATE exit_identity SET order_id=? WHERE cycle_id=? AND order_id IS NULL',
                                    (order_id,cycle_id)).rowcount
            if changed != 1:
                raise CycleBlocked('exit identity missing or already acknowledged')
            self.db.execute('COMMIT')
        except sqlite3.IntegrityError:
            self.db.execute('ROLLBACK')
            raise CycleBlocked('exit provider id already used') from None
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def record_verified_exit(self, cycle_id, *, order, position, inventory, order_observed_at,
                             position_observed_at, inventory_started_at, inventory_finished_at, now):
        row=self.read(cycle_id)
        if row['state'] != 'EXIT_PENDING':
            raise CycleBlocked('unexpected cycle state')
        identity=self.db.execute('SELECT client_id,order_id FROM exit_identity WHERE cycle_id=?',(cycle_id,)).fetchone()
        if not identity or not identity[1]:
            raise CycleBlocked('exit acknowledgement missing; reconcile without retry')
        valid=confirmed_exit(order=order,position=position,inventory=inventory,contract=row['contract'],
            order_id=identity[1],client_id=identity[0],order_observed_at=order_observed_at,
            position_observed_at=position_observed_at,inventory_started_at=inventory_started_at,
            inventory_finished_at=inventory_finished_at,now=now)
        self._transition(cycle_id, {'EXIT_PENDING'}, 'CLOSED' if valid else 'HALTED',
                         'exit verified' if valid else 'exit evidence not reconciled')
        return valid

    def record_flat_confirmation(self, cycle_id, *, position, open_orders, open_stop_orders,
                                 all_positions_flat, exchange_confirmed):
        flat = (_quantity(position) == 0 and type(open_orders) is int and open_orders == 0
                and type(open_stop_orders) is int and open_stop_orders == 0
                and all_positions_flat is True and exchange_confirmed is True)
        self._transition(cycle_id, {'EXIT_PENDING','EMERGENCY_EXIT_PENDING'}, 'CLOSED' if flat else 'HALTED',
                         'flat confirmed' if flat else 'flat not confirmed')
        return flat


