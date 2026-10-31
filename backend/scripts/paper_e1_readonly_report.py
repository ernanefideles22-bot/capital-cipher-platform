"""E1 read-only PostgreSQL PAPER forensic export (not an execution service).

Run only from an authorized diagnostic environment with read permissions.
Uses SET TRANSACTION READ ONLY, bounded result sets, no schemas/DDL/DML
and never imports OMS / exchange adapters. Does not log DB credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import desc, or_, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database.models import (
    AgentOutputModel, AuditLogModel, DecisionModel, PaperOrderModel,
    RiskCheckModel,
)
from app.strategy.paper_forensics_e1 import (
    AgentObservation, PaperTrace, analyze_paper_trades,
)


def to_float(value):
    return float(value) if value is not None else None


def _optional_number(data: dict, key: str):
    v = data.get(key)
    if v is None:
        return None
    return float(v)


def read_only_url(value: str) -> str:
    if not value:
        raise ValueError("DATABASE_URL required")
    if value.startswith("postgresql+asyncpg://"):
        return value
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+asyncpg://", 1)
    raise ValueError("E1 extractor requires PostgreSQL; SQLite/offline files unsupported")


async def export_forensics(
    database_url: str, *, lookback_days: int = 30, order_limit: int = 250,
    agent_limit: int = 20000,
) -> dict:
    if not 1 <= lookback_days <= 365:
        raise ValueError("Invalid lookback days")
    if not 1 <= order_limit <= 1000 or not 1 <= agent_limit <= 100000:
        raise ValueError("Invalid bounded audit size")
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    engine = create_async_engine(
        read_only_url(database_url), pool_size=1, max_overflow=0,
        pool_pre_ping=True, connect_args={"timeout": 10},
    )
    try:
        async with engine.connect() as conn:
            # The first statement of this database transaction MUST be
            # READ ONLY; queries and SET LOCAL below cannot write.
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text("SET LOCAL statement_timeout = '20000ms'"))
            query = select(PaperOrderModel).where(
                or_(
                    (PaperOrderModel.status == "CLOSED")
                    & (PaperOrderModel.closed_at >= cutoff),
                    (PaperOrderModel.status == "FILLED")
                    & (PaperOrderModel.opened_at >= cutoff),
                )
            ).order_by(desc(PaperOrderModel.created_at)).limit(order_limit + 1)
            orders = list((await conn.execute(query)).scalars())
            truncated = len(orders) > order_limit
            orders = orders[:order_limit]
            if not orders:
                report = analyze_paper_trades(())
                return {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "source": "POSTGRESQL_READ_ONLY",
                    "lookback_days": lookback_days,
                    "order_sample_truncated": False,
                    "agents_sample_truncated": False,
                    "report": asdict(report),
                    "caveat": "Zero visible rows is not evidence of zero trades; verify archive/retention and RLS.",
                }
            decisions_ids = {x.decision_id for x in orders}
            risk_ids = {x.risk_check_id for x in orders}
            corrs = {x.correlation_id for x in orders}
            order_ids = {x.id for x in orders}
            decisions = {
                x.id: x for x in (await conn.execute(
                    select(DecisionModel).where(DecisionModel.id.in_(decisions_ids))
                )).scalars()
            }
            risks = {
                x.id: x for x in (await conn.execute(
                    select(RiskCheckModel).where(RiskCheckModel.id.in_(risk_ids))
                )).scalars()
            }
            audits = list((await conn.execute(
                select(AuditLogModel).where(
                    AuditLogModel.entity_type == "paper_order",
                    AuditLogModel.audit_type == "PAPER_ORDER_CLOSED",
                    AuditLogModel.entity_id.in_(order_ids),
                ).order_by(desc(AuditLogModel.created_at))
            )).scalars())
            audit_by_order = {}
            for a in audits:
                audit_by_order.setdefault(a.entity_id, a)
            agents = list((await conn.execute(
                select(AgentOutputModel).where(
                    AgentOutputModel.correlation_id.in_(corrs),
                    AgentOutputModel.created_at >= cutoff - timedelta(days=8),
                ).order_by(desc(AgentOutputModel.created_at)).limit(agent_limit + 1)
            )).scalars())
            agents_truncated = len(agents) > agent_limit
            agents = agents[:agent_limit]
            by_corr: dict[str, list] = {}
            for a in agents:
                by_corr.setdefault(a.correlation_id, []).append(a)
            traces = []
            for o in orders:
                d = decisions.get(o.decision_id)
                risk = risks.get(o.risk_check_id)
                closing_audit = audit_by_order.get(o.id)
                # Independent equality checks: shared correlation alone is
                # insufficient evidence for a linked decision or risk.
                decision_valid = bool(
                    d is not None and d.correlation_id == o.correlation_id
                    and d.symbol == o.symbol and d.timeframe == o.timeframe
                    and d.created_at <= (o.opened_at or o.created_at)
                )
                risk_valid = bool(
                    risk is not None and risk.decision_id == o.decision_id
                    and risk.correlation_id == o.correlation_id
                    and risk.created_at <= (o.opened_at or o.created_at)
                )
                audit_valid = bool(
                    closing_audit is not None
                    and closing_audit.correlation_id == o.correlation_id
                    and o.closed_at is not None
                    and closing_audit.created_at >= o.closed_at
                )
                payload = closing_audit.payload if audit_valid else {}
                output = []
                # Agent outputs are *opinions* observed before order opening,
                # not causal P&L attribution. Reject post-entry information.
                for agent in by_corr.get(o.correlation_id, []):
                    if o.opened_at is not None and agent.created_at > o.opened_at:
                        continue
                    if agent.signal not in ("BUY", "SELL", "HOLD", "WAIT", "BLOCK", "NEUTRAL"):
                        continue
                    output.append(AgentObservation(
                        agent=agent.agent_name,
                        signal=agent.signal,
                        observed_at=agent.created_at,
                        confidence=agent.confidence,
                    ))
                traces.append(PaperTrace(
                    order_id=o.id, decision_id=o.decision_id,
                    risk_check_id=o.risk_check_id,
                    correlation_id=o.correlation_id,
                    symbol=o.symbol,
                    exchange=o.exchange,
                    timeframe=o.timeframe or "UNKNOWN",
                    strategy=o.strategy, side=o.side,
                    status=o.status, entry_price=float(o.entry_price),
                    position_notional=float(o.position_size),
                    opened_at=o.opened_at or o.created_at,
                    closed_at=o.closed_at, pnl_net=to_float(o.pnl),
                    stop_loss=to_float(o.stop_loss),
                    take_profit=to_float(o.take_profit),
                    fees_estimated=to_float(o.fees_estimated),
                    slippage_estimated=to_float(o.slippage_estimated),
                    exit_price=_optional_number(payload, "exit_price"),
                    exit_reason=payload.get("exit_reason"),
                    decision_action=d.candidate_action if decision_valid else None,
                    decision_confidence=d.confidence if decision_valid else None,
                    decision_trace_present=decision_valid,
                    risk_trace_present=risk_valid,
                    risk_approved=risk.approved if risk_valid else None,
                    risk_reward_declared=to_float(risk.risk_reward) if risk_valid else None,
                    audit_close_present=audit_valid,
                    agents=tuple(output),
                ))
            report = analyze_paper_trades(traces)
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "POSTGRESQL_READ_ONLY",
                "lookback_days": lookback_days,
                "order_sample_truncated": truncated,
                "agents_sample_truncated": agents_truncated,
                "tables_read": [
                    "capital_cipher.paper_orders",
                    "capital_cipher.decisions",
                    "capital_cipher.risk_checks",
                    "capital_cipher.agent_outputs",
                    "capital_cipher.audit_logs",
                ],
                "report": asdict(report),
                "caveat": "Hot-window retention and archived records may require a second, independently verified export.",
            }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--order-limit", type=int, default=250)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = asyncio.run(export_forensics(
        os.environ.get("DATABASE_URL", ""),
        lookback_days=args.lookback_days,
        order_limit=args.order_limit,
    ))
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite an earlier forensic snapshot.
    with destination.open("x", encoding="utf-8") as file:
        json.dump(result, file, sort_keys=True, indent=2, allow_nan=False)
        file.write("\n")
    summary = result["report"]
    print(json.dumps({
        "source": result["source"],
        "mode": summary["mode"],
        "closed_trades": summary["closed_trades"],
        "open_trades": summary["open_trades"],
        "net_pnl": summary["net_pnl"],
        "order_sample_truncated": result["order_sample_truncated"],
        "agent_attribution_verified": False,
        "execution_authorized": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
