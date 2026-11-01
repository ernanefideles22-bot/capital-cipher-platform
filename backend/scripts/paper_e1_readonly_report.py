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

from sqlalchemy import desc, or_, select, text, tuple_, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.models import (
    AgentOutputModel, AgentExecutionJobModel, AgentForecastModel,
    AgentForecastOutcomeModel, AuditLogModel, DecisionModel, PaperOrderModel,
    RiskCheckModel, CandleObservationModel,
)
from app.strategy.paper_forensics_e1 import (
    AgentObservation, PaperTrace, analyze_paper_trades,
)
from app.strategy.paper_evidence_integrity_e11 import inspect_evidence
from app.strategy.paper_net_economics_e13 import analyze_cost_diagnostics
from app.strategy.paper_shadow_cohorts_e14 import describe_shadow_cohorts
from app.strategy.paper_candidate_coverage_e15 import describe_candidate_visibility
from app.strategy.paper_decision_context_e17 import inspect_decision_context
from app.strategy.paper_agent_quality_e22 import evaluate_runtime_quality
from app.strategy.paper_agent_cohort_e23 import (
    cohort_query, summarize_cohort_rows,
)
from app.strategy.paper_market_provenance_e24 import (
    independence_query, event_probe_query, summarize_market_evidence,
    EVENT_PROBE_LIMIT, OBSERVATION_LIMIT,
)
from app.strategy.paper_future_agent_cohort_e26 import (
    readiness as e26_readiness,
    per_agent_query as e26_per_agent_query,
    summarize as e26_agent_summary,
)
from app.strategy.paper_future_market_readiness_e26 import (
    recent_warmup_query, summarize_warmup,
)
from app.strategy.paper_future_market_diagnostics_e261 import diagnose_candle_rows
from app.strategy.prospective_core_gate_e26 import MARKETS as E26_MARKETS


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
        async with AsyncSession(engine, expire_on_commit=False) as session:
            # AsyncSession materializes mapped ORM models from SELECT(Model).
            # AsyncConnection.execute() instead returns Core rows: .scalars()
            # would yield the first column (typically a string ID), not a model.
            # First SQL statement: a stable REPEATABLE READ snapshot is
            # required while the retention worker mutates historical tables.
            # READ ONLY prevents writes; session close rolls back.
            await session.execute(text(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            ))
            readonly = await session.scalar(text("SHOW transaction_read_only"))
            isolation = await session.scalar(text("SHOW transaction_isolation"))
            if str(readonly).lower() not in {"on", "true"}:
                raise RuntimeError("E1_TRANSACTION_NOT_READ_ONLY")
            if str(isolation).lower() != "repeatable read":
                raise RuntimeError("E1_TRANSACTION_NOT_REPEATABLE_READ")
            await session.execute(text("SET LOCAL statement_timeout = '20000ms'"))
            query = select(PaperOrderModel).where(
                or_(
                    (PaperOrderModel.status == "CLOSED")
                    & (PaperOrderModel.closed_at >= cutoff),
                    (PaperOrderModel.status == "FILLED")
                    & (PaperOrderModel.opened_at >= cutoff),
                )
            ).order_by(desc(PaperOrderModel.created_at)).limit(order_limit + 1)
            orders = list((await session.execute(query)).scalars())
            truncated = len(orders) > order_limit
            orders = orders[:order_limit]
            # E1.5: bounded historical candidate coverage, including HOLD/WAIT
            # outcomes that do not produce PAPER orders. This stays in the
            # same REPEATABLE READ / READ ONLY snapshot as the trade audit.
            decision_sample_limit = 1000
            candidate_decisions = list((await session.execute(
                select(DecisionModel).where(
                    DecisionModel.created_at >= cutoff
                ).order_by(desc(DecisionModel.created_at)).limit(
                    decision_sample_limit + 1
                )
            )).scalars())
            candidate_sample_truncated = len(candidate_decisions) > decision_sample_limit
            candidate_decisions = candidate_decisions[:decision_sample_limit]
            candidate_ids = {d.id for d in candidate_decisions}
            candidate_corrs = {d.correlation_id for d in candidate_decisions}
            # The limits here are independent from the order/agent sample.
            # Missing related evidence is visibility, not proof of no event.
            candidate_risks = list((await session.execute(
                select(RiskCheckModel).where(
                    RiskCheckModel.decision_id.in_(candidate_ids)
                ).limit(2001)
            )).scalars()) if candidate_ids else []
            candidate_orders = list((await session.execute(
                select(PaperOrderModel).where(
                    PaperOrderModel.decision_id.in_(candidate_ids)
                ).limit(2001)
            )).scalars()) if candidate_ids else []
            candidate_agents = list((await session.execute(
                select(AgentOutputModel).where(
                    AgentOutputModel.correlation_id.in_(candidate_corrs)
                ).limit(10001)
            )).scalars()) if candidate_corrs else []
            # Modern runtime persists agent outputs inside execution jobs;
            # legacy agent_outputs is not an exhaustive runtime evidence table.
            # Only the newest bounded rows are read from the same snapshot.
            candidate_jobs = list((await session.execute(
                select(AgentExecutionJobModel).where(
                    AgentExecutionJobModel.correlation_id.in_(candidate_corrs)
                ).order_by(desc(AgentExecutionJobModel.created_at)).limit(10001)
            )).scalars()) if candidate_corrs else []
            candidate_coverage = describe_candidate_visibility(
                candidate_decisions,
                candidate_risks[:2000],
                candidate_orders[:2000],
                candidate_agents[:10000],
                sampled_limit=decision_sample_limit,
                has_more_decisions=candidate_sample_truncated,
                execution_jobs=candidate_jobs[:10000],
            )
            candidate_coverage["decision_context"] = inspect_decision_context(
                candidate_decisions, candidate_risks[:2000],
                candidate_orders[:2000],
            )
            candidate_coverage["related_evidence_sample_truncated"] = {
                "risk_checks": len(candidate_risks) > 2000,
                "paper_orders": len(candidate_orders) > 2000,
                "agent_outputs": len(candidate_agents) > 10000,
                "agent_execution_jobs": len(candidate_jobs) > 10000,
            }
            # E2.2 historical agent-quality preflight: no writes, no external
            # market calls and no OMS/agent execution. Check optional schema
            # tables before selecting so absence does not abort the snapshot.
            forecast_source_visible = (
                await session.scalar(select(text(
                    "to_regclass('capital_cipher.agent_forecasts')"
                )))
            ) is not None
            outcome_source_visible = (
                await session.scalar(select(text(
                    "to_regclass('capital_cipher.agent_forecast_outcomes')"
                )))
            ) is not None
            job_source_visible = (
                await session.scalar(select(text(
                    "to_regclass('capital_cipher.agent_execution_jobs')"
                )))
            ) is not None
            forecast_limit = 3000
            e22_forecasts = list((await session.execute(
                select(AgentForecastModel)
                .where(AgentForecastModel.created_at >= cutoff)
                .order_by(desc(AgentForecastModel.created_at))
                .limit(forecast_limit + 1)
            )).scalars()) if forecast_source_visible and outcome_source_visible else []
            e22_forecasts_truncated = len(e22_forecasts) > forecast_limit
            e22_forecasts = e22_forecasts[:forecast_limit]
            e22_ids = {x.forecast_id for x in e22_forecasts}
            e22_outcomes = list((await session.execute(
                select(AgentForecastOutcomeModel)
                .where(AgentForecastOutcomeModel.forecast_id.in_(e22_ids))
                .limit(forecast_limit + 1)
            )).scalars()) if e22_ids else []
            # Independent of decision correlations; observe runtime jobs that
            # are never promoted into the legacy agent_outputs table.
            job_limit = 5000
            e22_jobs = list((await session.execute(
                select(AgentExecutionJobModel)
                .where(AgentExecutionJobModel.created_at >= cutoff)
                .order_by(desc(AgentExecutionJobModel.created_at))
                .limit(job_limit + 1)
            )).scalars()) if job_source_visible else []
            e22_jobs_truncated = len(e22_jobs) > job_limit
            e22_quality = evaluate_runtime_quality(
                e22_forecasts, e22_outcomes, e22_jobs[:job_limit],
                forecasts_truncated=e22_forecasts_truncated,
                jobs_truncated=e22_jobs_truncated,
                forecast_table_visible=forecast_source_visible,
                outcome_table_visible=outcome_source_visible,
                jobs_table_visible=job_source_visible,
            )
            # E2.3: a single GROUP BY returns a bounded number of cohort
            # aggregates over seven days; no unbounded forecast materialization.
            # If either source is inaccessible, mark NOT_VISIBLE instead.
            cohort_now = datetime.now(timezone.utc)
            cohort_rows = list((await session.execute(
                cohort_query(cohort_now)
            )).all()) if forecast_source_visible and outcome_source_visible else []
            e23_quality = summarize_cohort_rows(
                cohort_rows,
                tables_visible=forecast_source_visible and outcome_source_visible,
                now=cohort_now,
            )
            # E2.4: census of DISTINCT market target events, not 300
            # independent observations just because 300 agents shared a bar.
            # All evidence remains in the existing READ ONLY snapshot.
            e24_candle_visible = (
                await session.scalar(select(text(
                    "to_regclass('capital_cipher.candle_observations')"
                )))
            ) is not None
            e24_series = list((await session.execute(
                independence_query(cohort_now)
            )).all()) if forecast_source_visible and outcome_source_visible else []
            e24_events = list((await session.execute(
                event_probe_query(cohort_now)
            )).all()) if forecast_source_visible and outcome_source_visible else []
            e24_sample = e24_events[:EVENT_PROBE_LIMIT]
            e24_keys = {
                (str(row.symbol), str(row.timeframe), when)
                for row in e24_sample
                for when in (row.forecast_at, row.target_at)
            }
            e24_observations = list((await session.execute(
                select(CandleObservationModel)
                .where(tuple_(
                    CandleObservationModel.symbol,
                    CandleObservationModel.timeframe,
                    CandleObservationModel.closed_at,
                ).in_(e24_keys))
                .limit(OBSERVATION_LIMIT)
            )).scalars()) if e24_candle_visible and e24_keys else []
            # Unlike forecast.created_at, job.created_at is the actual
            # runtime submission timestamp (see AgentRuntime.submit()).
            # It is the first downstream event after save_candle() in the
            # orchestrator. Never interpret candle.received_at as an INSERT.
            e24_corrs = {
                row.sample_correlation for row in e24_sample
                if row.correlation_count == 1 and row.sample_correlation
            }
            e24_jobs = list((await session.execute(
                select(
                    AgentExecutionJobModel.correlation_id,
                    func.min(AgentExecutionJobModel.created_at).label(
                        "first_created"
                    ),
                )
                .where(
                    AgentExecutionJobModel.correlation_id.in_(e24_corrs)
                )
                .group_by(AgentExecutionJobModel.correlation_id)
                .limit(EVENT_PROBE_LIMIT + 1)
            )).all()) if job_source_visible and e24_corrs else []
            e24_market = summarize_market_evidence(
                e24_series, e24_events, e24_observations,
                observation_table_visible=e24_candle_visible,
                first_job_created_by_correlation={
                    x.correlation_id: x.first_created for x in e24_jobs
                },
                job_table_visible=job_source_visible,
                now=cohort_now,
            )
            e24_market["forecast_and_outcome_tables_visible"] = (
                forecast_source_visible and outcome_source_visible
            )
            # E2.6: after the PRE-FROZEN UTC campaign start only, observe
            # existing 300-agent PAPER/SHADOW forecasts and immutable job
            # identities. No workers, scheduling, writes or OMS permission.
            # Until the future target window, do not even query candidates.
            e26_sources = (
                forecast_source_visible and outcome_source_visible
                and job_source_visible
            )
            e26_status = e26_readiness(
                cohort_now, sources_visible=e26_sources
            )
            e26_rows = list((await session.execute(
                e26_per_agent_query(cohort_now)
            )).all()) if e26_sources and e26_status != (
                "FUTURE_WINDOW_NOT_STARTED"
            ) else []
            e26_forward = e26_agent_summary(
                e26_rows, now=cohort_now, sources_visible=e26_sources
            )
            # Existing 300-agent database already stores received + recorded
            # market candles. Before creating a new paid collector service,
            # determine whether 300 complete BINANCE-labelled warmup candles
            # are visible for each frozen forward-market timeframe.
            e26_market_series = {}
            for e26_symbol, e26_tf in E26_MARKETS:
                e26_last_bars = list((await session.execute(
                    recent_warmup_query(e26_symbol,e26_tf,now=cohort_now)
                )).scalars()) if e24_candle_visible else []
                e26_market_series[e26_symbol+"_"+e26_tf] = summarize_warmup(
                    e26_last_bars,symbol=e26_symbol,timeframe=e26_tf,
                    now=cohort_now,table_visible=e24_candle_visible,
                )
                # E2.6.1 only interprets this SAME bounded SELECT snapshot.
                # It cannot backfill, alter quality gates, or attest source.
                e26_market_series[e26_symbol+"_"+e26_tf]["diagnostics_e261"] = (
                    diagnose_candle_rows(
                        e26_last_bars, symbol=e26_symbol, timeframe=e26_tf,
                        now=cohort_now,
                    )
                )
            e26_market_readiness = {
                "mode":"E26_EXISTING_STAGING_MARKET_STORAGE_READ_ONLY_PRECHECK",
                "source":"POSTGRESQL_ONLY_NO_NEW_WORKER",
                "three_series":e26_market_series,
                "complete_internal_warmup_count":sum(
                    x.get("warmup_complete",False)
                    for x in e26_market_series.values()
                ),
                "all_market_feeds_verified_external":False,
                "separate_foreign_collector_deployed":False,
                "future_data_prospective_proof":False,
                "execution_authorized":False,
            }
            if not orders:
                report = analyze_paper_trades(())
                return {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "source": "POSTGRESQL_READ_ONLY",
                    "lookback_days": lookback_days,
                    "order_sample_truncated": False,
                    "agents_sample_truncated": False,
                    "archive_reconciliation": "NOT_PERFORMED",
                    "agent_history_coverage": "UNVERIFIED_MAY_BE_PRUNED",
                    "evidence_integrity": {"status": "NOT_EVALUATED_NO_VISIBLE_ORDERS"},
                    "cost_diagnostics": analyze_cost_diagnostics([]),
                    "shadow_cohorts": describe_shadow_cohorts(
                        asdict(report), analyze_cost_diagnostics([])
                    ),
                    "candidate_coverage": candidate_coverage,
                    "agent_quality": e22_quality,
                    "agent_cohort": e23_quality,
                    "market_provenance": e24_market,
                    "future_agent_cohort": e26_forward,
                    "future_market_readiness": e26_market_readiness,
                    "report": asdict(report),
                    "caveat": "Zero visible rows is not evidence of zero trades; verify archive/retention and RLS.",
                }
            decisions_ids = {x.decision_id for x in orders}
            risk_ids = {x.risk_check_id for x in orders}
            corrs = {x.correlation_id for x in orders}
            order_ids = {x.id for x in orders}
            decisions = {
                x.id: x for x in (await session.execute(
                    select(DecisionModel).where(DecisionModel.id.in_(decisions_ids))
                )).scalars()
            }
            risks = {
                x.id: x for x in (await session.execute(
                    select(RiskCheckModel).where(RiskCheckModel.id.in_(risk_ids))
                )).scalars()
            }
            # A table can have some visible rows while individual historical
            # IDs remain hidden by RLS or are missing. Neither means approval
            # was bypassed; report both signals without asserting causality.
            any_decision_visible = (
                await session.scalar(select(DecisionModel.id).limit(1))
            ) is not None
            any_risk_visible = (
                await session.scalar(select(RiskCheckModel.id).limit(1))
            ) is not None
            audits = list((await session.execute(
                select(AuditLogModel).where(
                    AuditLogModel.entity_type == "paper_order",
                    AuditLogModel.audit_type == "PAPER_ORDER_CLOSED",
                    AuditLogModel.entity_id.in_(order_ids),
                ).order_by(desc(AuditLogModel.created_at))
            )).scalars())
            # Choose the newest *verified* closure, not simply the latest
            # event with this entity ID. A newer unrelated/malformed event
            # must not hide a valid earlier closure (false missing-audit flag).
            orders_by_id = {o.id: o for o in orders}
            audit_by_order = {}
            for a in audits:
                order = orders_by_id.get(a.entity_id)
                if (
                    order is None or order.status != "CLOSED"
                    or order.closed_at is None
                    or a.correlation_id != order.correlation_id
                    or a.created_at < order.closed_at
                ):
                    continue
                previous = audit_by_order.get(a.entity_id)
                if previous is None or a.created_at > previous.created_at:
                    audit_by_order[a.entity_id] = a
            evidence_integrity = inspect_evidence(
                orders, decisions, risks, audit_by_order,
                any_decision_visible=any_decision_visible,
                any_risk_visible=any_risk_visible,
            )
            # Research-only scenario screen using PAPER position notional,
            # actual fill, planned stop and target. No OMS/risk mutations.
            cost_diagnostics = analyze_cost_diagnostics(orders)
            agents = list((await session.execute(
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
                # opened_at is the candle's market timestamp, often earlier
                # than the risk decision. Use order.created_at, the persisted
                # order creation clock, to test evidence processing chronology.
                decision_valid = bool(
                    d is not None and d.correlation_id == o.correlation_id
                    and d.symbol == o.symbol and d.timeframe == o.timeframe
                    and d.created_at <= o.created_at
                )
                risk_valid = bool(
                    risk is not None and risk.decision_id == o.decision_id
                    and risk.correlation_id == o.correlation_id
                    and risk.created_at <= o.created_at
                )
                audit_valid = bool(
                    closing_audit is not None
                    and closing_audit.correlation_id == o.correlation_id
                    and o.closed_at is not None
                    and closing_audit.created_at >= o.closed_at
                )
                payload = closing_audit.payload if audit_valid else {}
                output = []
                # Agent outputs are observational opinions generated before
                # actual order construction. The candle timestamp (opened_at)
                # is a historical market clock, not the processing clock.
                # This does not establish causal agent P&L attribution.
                for agent in by_corr.get(o.correlation_id, []):
                    if agent.created_at > o.created_at:
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
                    order_created_at=o.created_at,
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
            shadow_cohorts = describe_shadow_cohorts(
                asdict(report), cost_diagnostics,
            )
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "POSTGRESQL_READ_ONLY",
                "lookback_days": lookback_days,
                "order_sample_truncated": truncated,
                "agents_sample_truncated": agents_truncated,
                "archive_reconciliation": "NOT_PERFORMED",
                "agent_history_coverage": "UNVERIFIED_MAY_BE_PRUNED",
                "evidence_integrity": evidence_integrity,
                "cost_diagnostics": cost_diagnostics,
                "shadow_cohorts": shadow_cohorts,
                "candidate_coverage": candidate_coverage,
                "agent_quality": e22_quality,
                "agent_cohort": e23_quality,
                "market_provenance": e24_market,
                "future_agent_cohort": e26_forward,
                "future_market_readiness": e26_market_readiness,
                "tables_read": [
                    "capital_cipher.paper_orders",
                    "capital_cipher.decisions",
                    "capital_cipher.risk_checks",
                    "capital_cipher.agent_outputs",
                    "capital_cipher.agent_execution_jobs",
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
