"""Offline read-only financial review. Never imported by the hosted service."""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.strategy.gate_s3_durable_research import (
    GENESIS_HASH, MAX_OBSERVATIONS_PER_REPLAY, S3JournalBlocked, _event_hash,
)
from app.strategy.gate_s3_equity_valuation import read_only_equity_report


def _closed_accounts(path, verified):
    """Reject a changed prefix between the verified replay and trade extraction.

    last_closed_trade is a repeated snapshot, not a stream of new fills.
    Count only completed_trades increments and reconcile every resulting book.
    """
    heads, counts, books = {}, {}, {}
    observations = 0
    with closing(sqlite3.connect(Path(path).absolute().as_uri()+"?mode=ro",
                                uri=True, isolation_level=None)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        for symbol, payload, outcome, previous, digest in db.execute(
                "SELECT symbol,payload_json,outcome_json,previous_hash,event_hash "
                "FROM s3_observations ORDER BY seq"):
            observations += 1
            if (observations > MAX_OBSERVATIONS_PER_REPLAY
                    or previous != heads.get(symbol, GENESIS_HASH)
                    or digest != _event_hash(previous, payload, outcome)):
                raise S3JournalBlocked("Financial review prefix cannot be verified")
            heads[symbol] = digest
            counts[symbol] = counts.get(symbol, 0)+1
            for a in json.loads(outcome)["accounts"]:
                key = (symbol, a["strategy"], a["arm"])
                b = books.setdefault(key, {"count": 0, "nets": [], "exits": {}})
                delta = a["completed_trades"]-b["count"]
                if delta not in (0, 1):
                    raise S3JournalBlocked("Unexpected completed-trade increment")
                if delta:
                    t = a["last_closed_trade"]
                    if not t["hypothetical"] or t["real_exchange_fill"]:
                        raise S3JournalBlocked("Unexpected real fill in research journal")
                    net, gross, fees, funding = (
                        t[k] for k in ("net_pnl", "gross_after_slippage_before_fees",
                                       "fees", "funding"))
                    if (not all(math.isfinite(v) for v in (net, gross, fees, funding))
                            or not math.isclose(net, gross-fees-funding, abs_tol=1e-7)):
                        raise S3JournalBlocked("Closed trade costs do not reconcile")
                    b["nets"].append(net)
                    reason = t["exit_reason"]
                    b["exits"][reason] = b["exits"].get(reason, 0)+1
                b["count"] = a["completed_trades"]
                b["snapshot"] = a
        stored = db.execute("SELECT symbol,event_hash,event_count FROM s3_heads").fetchall()
        if (len(stored) != len(heads)
                or any(heads.get(s) != h or counts.get(s) != n for s, h, n in stored)
                or observations != verified["observations"]
                or heads != verified["verified_hash_heads"]):
            raise S3JournalBlocked("Journal advanced or changed between review snapshots; retry")
    return books


def financial_review(path: str | Path, *, as_of: datetime | None = None) -> dict:
    verified = read_only_equity_report(path)
    try:
        books = _closed_accounts(path, verified)
        expected_keys = {(a["symbol"], a["strategy"], a["arm"])
                         for a in verified["accounts"]}
        if set(books) != expected_keys:
            raise S3JournalBlocked("Financial review book mismatch")
        if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
            raise S3JournalBlocked("Timezone-aware as_of required")
        accounts = []
        for a in verified["accounts"]:
            b = books[(a["symbol"], a["strategy"], a["arm"])]
            n, nets, snap = b["count"], b["nets"], b["snapshot"]
            if (n != a["completed_trades"]
                    or not math.isclose(snap["net_pnl"], a["realized_net_pnl"], abs_tol=1e-7)
                    or not math.isclose(sum(nets), a["realized_net_pnl"],
                                        abs_tol=1e-7*max(1, n))):
                raise S3JournalBlocked("Realized PnL does not reconcile")
            gains = sum(v for v in nets if v > 0)
            losses = -sum(v for v in nets if v < 0)
            row = {**a,
                   "closed_gross_after_slippage_before_fees": snap["gross_after_slippage_before_fees"],
                   "closed_fees": snap["fees"], "closed_funding": snap["funding"],
                   "closed_net_wins": sum(v > 0 for v in nets),
                   "expectancy_net_per_closed_trade": round(a["realized_net_pnl"]/n, 8) if n else None,
                   "profit_factor_net": round(gains/losses, 8) if losses else None,
                   "profit_factor_status": "DEFINED" if losses else (
                       "NO_CLOSED_LOSSES" if n else "NO_CLOSED_TRADES"),
                   "exit_counts": dict(sorted(b["exits"].items())),
                   "freshness": "NOT_EVALUATED"}
            if as_of is not None:
                age = (as_of-datetime.fromisoformat(a["observation_received_at_utc"])).total_seconds()
                if age < 0:
                    raise S3JournalBlocked("as_of precedes observation")
                row.update(age_seconds=age, freshness="CURRENT" if age <= 1800 else "STALE")
            accounts.append(row)
        pairs = []
        for symbol, strategy in sorted({(a["symbol"], a["strategy"]) for a in accounts}):
            arms = {a["arm"]: a for a in accounts
                    if (a["symbol"], a["strategy"]) == (symbol, strategy)}
            base, filtered = arms["STANDALONE"], arms["S1_CONFLUENCE"]
            sufficient = (verified["eligible_windows"].get(symbol, {}).get("post_freeze_candles", 0) >= 96
                          and min(base["completed_trades"], filtered["completed_trades"]) >= 30)
            pairs.append({"symbol": symbol, "strategy": strategy,
                          "standalone_closed_net": base["realized_net_pnl"],
                          "confluence_closed_net": filtered["realized_net_pnl"],
                          "cash_closed_net": 0.,
                          "confluence_minus_standalone_closed_net": round(
                              filtered["realized_net_pnl"]-base["realized_net_pnl"], 8),
                          "sample_status": "PRELIMINARY_DESCRIPTIVE_ONLY" if sufficient else "INSUFFICIENT"})
        return {**verified, "review_model": "S3_READ_ONLY_FINANCIAL_REVIEW_V1",
                "accounts": accounts, "closed_pnl_comparisons": pairs,
                "as_of_utc": as_of.astimezone(timezone.utc).isoformat() if as_of else None,
                "profit_factor_basis": "SUM_POSITIVE_NET_TRADES_OVER_ABS_SUM_NEGATIVE_NET_TRADES_ROUNDED_8DP",
                "slippage_accounting": "ALREADY_IN_GROSS_AND_NET; NOT_SUBTRACTED_TWICE",
                "statistical_significance_proven": False, "stress_validation_proven": False}
    except S3JournalBlocked:
        raise
    except (sqlite3.Error, ValueError, KeyError, TypeError, OverflowError):
        raise S3JournalBlocked("Cannot verify S3 financial review") from None


def render_markdown(report: dict) -> str:
    def number(value):
        return "indisponível" if value is None else f"{value:.8f}"
    lines = ["# Revisão financeira S3 — pesquisa hipotética",
             f"Observações verificadas: {report['observations']}. Estado: {report['status']}.",
             "Contas independentes; não representam uma carteira. Caixa: 0.",
             "", "| Mercado | Estratégia | Braço | Encerradas | Líquido encerrado | Expectativa | PF líquido | Saída aberta estimada | DD fechamento % |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for a in report["accounts"]:
        lines.append("| "+" | ".join(map(str, [a["symbol"], a["strategy"], a["arm"],
            a["completed_trades"], number(a["realized_net_pnl"]),
            number(a["expectancy_net_per_closed_trade"]), number(a["profit_factor_net"]),
            number(a["open_net_exit_estimate"]), number(a["candle_close_max_drawdown_pct"])]))+" |")
    lines += ["", "Custos e comparações por conta estão no JSON. Slippage já integra preços e PnL; não é descontado novamente.",
              "PF indisponível quando não há perdas encerradas ou não há operações encerradas; isso não demonstra lucro.",
              "Posições abertas são estimativas no último candle registrado. Sem as_of explícito, freshness não é avaliada.",
              "96 candles pós-freeze e 30 encerramentos em cada braço são apenas piso descritivo, sem aprovação de estratégia.",
              "Não mede drawdown intrabar, fills reais, significância ou robustez sob estresse. Execução e lucratividade não comprovadas."]
    return "\n\n".join(lines[:3])+"\n"+"\n".join(lines[3:])+"\n"
