"""Persistent virtual-account equity baselines; never reset during process boot."""

from decimal import Decimal
from app.core.errors import SecurityError


class GateEquityBaseline:
    def __init__(self, journal):
        self.db = journal.db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS gate_equity_baseline (id INTEGER PRIMARY KEY CHECK(id=1), initial TEXT NOT NULL, peak TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS gate_daily_equity (day TEXT PRIMARY KEY, initial TEXT NOT NULL)"
        )

    def observe(self, day, equity):
        value = Decimal(str(equity))
        if not value.is_finite() or value <= 0:
            raise SecurityError("Gate equity baseline invalid")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO gate_equity_baseline VALUES (1,?,?)",
                (str(value), str(value)),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO gate_daily_equity VALUES (?,?)",
                (day.isoformat(), str(value)),
            )
            initial, peak = self.db.execute(
                "SELECT initial,peak FROM gate_equity_baseline WHERE id=1"
            ).fetchone()
            peak = max(value, Decimal(peak))
            self.db.execute(
                "UPDATE gate_equity_baseline SET peak=? WHERE id=1", (str(peak),)
            )
            daily = self.db.execute(
                "SELECT initial FROM gate_daily_equity WHERE day=?", (day.isoformat(),)
            ).fetchone()[0]
            self.db.execute("COMMIT")
            return {
                "initial": float(initial),
                "peak": float(peak),
                "daily_initial": float(daily),
            }
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
