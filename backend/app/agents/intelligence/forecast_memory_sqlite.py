"""Standalone durable research ledger for settled forecast observations.

Uses a dedicated SQLite database; never opens the platform trading database.
Append-only and idempotent by forecast_id. No OMS, execution or risk access.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path

from .forecast_scoring import ForecastObservation, ForecastScorecard, score_forecasts


class ForecastMemorySQLite:
    """Explicitly configured SQLite research store (no implicit production path)."""

    def __init__(self, database_path: str | Path):
        path = Path(database_path)
        if str(path) == ":memory:":
            raise ValueError("Persistent memory requires a file path")
        if not path.parent.is_dir():
            raise ValueError("Database parent directory must already exist")
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS settled_forecast_observations (
                forecast_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                model_version TEXT NOT NULL,
                bias TEXT NOT NULL,
                reference_price REAL NOT NULL,
                realized_price REAL NOT NULL,
                forecast_at TEXT NOT NULL,
                outcome_at TEXT NOT NULL,
                probability_up REAL
            )
        """)
        return connection

    def append(self, observation: ForecastObservation) -> bool:
        """Insert once. Identical replay is harmless; conflicting replay fails."""
        data = asdict(observation)
        with self._connect() as conn:
            existing = conn.execute(
                """SELECT forecast_id, symbol, timeframe, model_version, bias,
                          reference_price, realized_price, forecast_at, outcome_at,
                          probability_up
                   FROM settled_forecast_observations WHERE forecast_id = ?""",
                (observation.forecast_id,),
            ).fetchone()
            if existing is not None:
                columns = list(data)
                if dict(zip(columns, existing)) != data:
                    raise ValueError("Conflicting observation for forecast_id")
                return False
            conn.execute(
                """INSERT INTO settled_forecast_observations
                   (forecast_id, symbol, timeframe, model_version, bias,
                    reference_price, realized_price, forecast_at, outcome_at,
                    probability_up) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(data.values()),
            )
        return True

    def observations(
        self, *, symbol: str | None = None,
        model_version: str | None = None,
    ) -> list[ForecastObservation]:
        query = """SELECT forecast_id, symbol, timeframe, model_version, bias,
                          reference_price, realized_price, forecast_at, outcome_at,
                          probability_up FROM settled_forecast_observations WHERE 1=1"""
        parameters: list[str] = []
        if symbol is not None:
            query += " AND symbol = ?"
            parameters.append(symbol)
        if model_version is not None:
            query += " AND model_version = ?"
            parameters.append(model_version)
        query += " ORDER BY forecast_at, forecast_id"
        with self._connect() as conn:
            rows = conn.execute(query, parameters).fetchall()
        columns = list(ForecastObservation.__dataclass_fields__)
        return [ForecastObservation(**dict(zip(columns, row))) for row in rows]

    def scorecard(
        self, *, symbol: str | None = None,
        model_version: str | None = None,
        minimum_sample: int = 30,
    ) -> ForecastScorecard:
        return score_forecasts(
            self.observations(symbol=symbol, model_version=model_version),
            minimum_sample=minimum_sample,
        )
