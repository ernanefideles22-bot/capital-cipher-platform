"""Read-only, candle-close equity analysis of an existing S3 research journal.

Replays and verifies V1 outcomes without changing the frozen scorer or its
stored output. Marks are hypothetical exit estimates, not exchange mark prices
or liquidation/fill evidence. No capital, position or risk settings are changed.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict
from pathlib import Path

from app.strategy.gate_s2_replay import Friction, ResearchRisk, _adverse_execution
from app.strategy.gate_s2e_live_shadow import S2E_POLICY_COMMIT
from app.strategy.gate_s2d_forward import FROZEN_AT
from app.strategy.gate_s3_durable_research import (
    GENESIS_HASH, MAX_OBSERVATIONS_PER_REPLAY, SCHEMA_VERSION,
    SCORER_MODEL_VERSION, S3JournalBlocked, _canonical, _deserialize, _event_hash,
)
from app.strategy.gate_s3_forward_scorecard import ShadowForwardScorecard

VALUATION_VERSION = "S3_CANDLE_CLOSE_EXIT_ESTIMATE_V1"


def _account_value(book, candle, friction):
    """Use full-precision replay state; round only at the report boundary."""
    position = book.position
    gross = fees = funding = 0.0
    if position is not None:
        exit_price = _adverse_execution(
            candle.close, position.direction, opening=False, friction=friction)
        gross = (exit_price-position.entry)*position.quantity*position.direction
        fees = (position.entry+exit_price)*position.quantity*friction.fee_bps_side/10000
        # Intentionally matches the frozen V1 close model, including the minimum
        # one 8h interval. This is a disclosed assumption, not actual funding.
        elapsed = (candle.closed_at-book.entry_candle).total_seconds()
        intervals = max(1, math.ceil(max(0., elapsed)/28800))
        funding = position.entry*position.quantity*friction.funding_bps_8h/10000*intervals
    return gross, fees, funding, book.equity+gross-fees-funding


def read_only_equity_report(path: str | Path) -> dict:
    """Verify one transactionally consistent SQLite snapshot and value each arm.

    Only opens an existing regular file with mode=ro and query_only. Never
    initializes schema, repairs, appends, integrates with a service or connects
    to an exchange. SQLite may use its normal WAL read coordination files.
    Every symbol/hash/outcome/head is checked before any result is returned.
    """
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise S3JournalBlocked("Existing regular S3 research journal required")
    try:
        db = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, isolation_level=None)
    except sqlite3.Error:
        raise S3JournalBlocked("Cannot open S3 research journal read-only") from None
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise S3JournalBlocked("Research integrity check failed")
        schema = dict(db.execute("SELECT name,version FROM s3_schema"))
        if schema.get("forward_research") != SCHEMA_VERSION:
            raise S3JournalBlocked("Unsupported S3 research schema")
        model = json.loads(schema["model_parameters"])
        friction = Friction(**model["friction"])
        risk = ResearchRisk(**model["risk"])
        expected = {
            "scorer_model_version": SCORER_MODEL_VERSION,
            "strategy_source_commit": S2E_POLICY_COMMIT,
            "friction": asdict(friction), "risk": asdict(risk),
        }
        if _canonical(expected) != schema["model_parameters"]:
            raise S3JournalBlocked("Unsupported frozen research model")
        scorer = ShadowForwardScorecard(friction=friction, risk=risk)
        heads, counts, clocks, values, windows = {}, {}, {}, {}, {}
        observations = 0
        rows = db.execute("""SELECT seq,symbol,candle_closed_at,observed_at,
            payload_json,outcome_json,previous_hash,event_hash
            FROM s3_observations ORDER BY seq""")
        for seq, symbol, closed_at, received_at, payload, outcome, previous, digest in rows:
            observations += 1
            if observations > MAX_OBSERVATIONS_PER_REPLAY:
                raise S3JournalBlocked("Excessive research replay history")
            if (previous != heads.get(symbol, (0, GENESIS_HASH))[1]
                    or digest != _event_hash(previous, payload, outcome)):
                raise S3JournalBlocked("Broken research event hash chain")
            candle, shadow, clock = _deserialize(payload)
            if (candle.symbol != symbol or candle.closed_at.isoformat() != closed_at
                    or clock.isoformat() != received_at
                    or (symbol in clocks and clock <= clocks[symbol])):
                raise S3JournalBlocked("Research event identity or clock mismatch")
            result = scorer.observe(candle, shadow, observed_at=clock)
            if _canonical(result) != outcome:
                raise S3JournalBlocked("Historical scoring result changed")
            if candle.closed_at > FROZEN_AT:
                window = windows.setdefault(symbol, {
                    "post_freeze_candles": 0,
                    "first_eligible_candle_utc": closed_at,
                    "last_eligible_candle_utc": closed_at,
                })
                window["post_freeze_candles"] += 1
                window["last_eligible_candle_utc"] = closed_at
            for key, book in scorer._books.items():
                if key[0] != symbol:
                    continue
                gross, fees, funding, equity = _account_value(book, candle, friction)
                prior = values.get(key)
                peak = max(risk.initial_equity, equity,
                           prior["peak"] if prior else risk.initial_equity)
                drawdown = max(0., (1-equity/peak)*100)
                max_drawdown = max(drawdown, prior["max_drawdown"] if prior else 0.)
                values[key] = {
                    "peak": peak, "max_drawdown": max_drawdown,
                    "gross": gross, "fees": fees, "funding": funding,
                    "equity": equity, "close": candle.close,
                    "closed_at": closed_at, "observed_at": received_at,
                }
            heads[symbol] = (seq, digest)
            counts[symbol] = counts.get(symbol, 0)+1
            clocks[symbol] = clock
        stored_heads = db.execute(
            "SELECT symbol,last_seq,event_hash,event_count FROM s3_heads").fetchall()
        if (len(stored_heads) != len(heads)
                or any((seq, digest, n) != (*heads.get(symbol, (-1, "")), counts.get(symbol, 0))
                       for symbol, seq, digest, n in stored_heads)):
            raise S3JournalBlocked("Research journal head/row mismatch")
        accounts = []
        for key, value in sorted(values.items()):
            book = scorer._books[key]
            accounts.append({
                "symbol": key[0], "strategy": key[1], "arm": key[2],
                "initial_equity": risk.initial_equity,
                "realized_net_pnl": round(book.net, 8),
                "completed_trades": book.completed_trades,
                "open_position": book.position is not None,
                "close_price": value["close"],
                "candle_closed_at_utc": value["closed_at"],
                "observation_received_at_utc": value["observed_at"],
                "open_gross_after_slippage": round(value["gross"], 8),
                "open_estimated_roundtrip_fees": round(value["fees"], 8),
                "open_estimated_funding": round(value["funding"], 8),
                "open_net_exit_estimate": round(value["gross"]-value["fees"]-value["funding"], 8),
                "equity_including_open_exit_estimate": round(value["equity"], 8),
                "total_net_exit_estimate": round(value["equity"]-risk.initial_equity, 8),
                "candle_close_peak_equity": round(value["peak"], 8),
                "candle_close_max_drawdown_pct": round(value["max_drawdown"], 6),
                "realized_only_max_drawdown_pct": round(book.max_drawdown_pct, 6),
            })
        return {
            "valuation_model": VALUATION_VERSION, "observations": observations,
            "status": "RESEARCH_ONLY" if observations else "NO_OBSERVATIONS",
            "accounts": accounts,
            "verified_hash_heads": {s: h for s, (seq, h) in sorted(heads.items())},
            "friction_assumptions": asdict(friction),
            "risk_assumptions": asdict(risk),
            "policy_frozen_at_utc": FROZEN_AT.isoformat(),
            "eligible_windows": dict(sorted(windows.items())),
            "cash_benchmark_pnl": 0., "source_database_modified": False,
            "independent_accounts_not_portfolio": True,
            "valuation_basis": "LAST_RECORDED_CANDLE_CLOSE_HYPOTHETICAL_EXIT",
            "intrabar_drawdown_measured": False, "exchange_mark_price": False,
            "stale_at_present_time_possible": True,
            "real_execution_proven": False, "profitability_proven": False,
            "risk_policy_modified": False, "execution_authorized": False,
        }
    except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError) as exc:
        if isinstance(exc, S3JournalBlocked):
            raise
        raise S3JournalBlocked("Cannot verify S3 journal for equity valuation") from None
    finally:
        db.close()
