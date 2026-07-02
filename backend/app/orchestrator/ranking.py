"""Agent Ranking — report only in this phase (docs/27-agent-ranking.md).

Docs/27: in early phases the ranking is computed in reports and must NOT
automatically change decision weights. Weight adjustment requires governance,
auditing and a minimum sample (min_samples_for_weight_adjustment: 100).
"""

from __future__ import annotations

from typing import Any

MIN_SAMPLES_FOR_WEIGHT_ADJUSTMENT = 100


class AgentRankingService:
    def __init__(self, orchestrator, paper_engine) -> None:
        self._orchestrator = orchestrator
        self._paper = paper_engine

    def report(self) -> list[dict[str, Any]]:
        """Per-agent report: reliability, directional accuracy, overconfidence."""
        # Map correlation_id -> closed order pnl.
        pnl_by_correlation = {
            o.correlation_id: (o.pnl or 0.0) for o in self._paper.closed_orders
        }
        # Collect agent signals per decision from agent_summary.
        stats: dict[str, dict[str, Any]] = {}
        for decision in self._orchestrator.recent_decisions:
            pnl = pnl_by_correlation.get(decision.correlation_id)
            for entry in decision.agent_summary:
                name = entry.get("name")
                if not name:
                    continue
                agent_stats = stats.setdefault(
                    name,
                    {
                        "signals": {},
                        "confidence_sum": 0,
                        "confidence_count": 0,
                        "directional_hits": 0,
                        "directional_misses": 0,
                        "overconfident_losses": 0,
                    },
                )
                signal = entry.get("signal", "NEUTRAL")
                confidence = int(entry.get("confidence", 0))
                agent_stats["signals"][signal] = agent_stats["signals"].get(signal, 0) + 1
                agent_stats["confidence_sum"] += confidence
                agent_stats["confidence_count"] += 1
                if pnl is not None and signal in ("BUY", "SELL"):
                    aligned = (
                        signal == decision.candidate_action.value
                        if decision.candidate_action.value in ("BUY", "SELL")
                        else False
                    )
                    if aligned:
                        if pnl > 0:
                            agent_stats["directional_hits"] += 1
                        else:
                            agent_stats["directional_misses"] += 1
                            if confidence >= 85:
                                agent_stats["overconfident_losses"] += 1

        report: list[dict[str, Any]] = []
        for name, agent in self._orchestrator.agents.items():
            health = agent.health()
            agent_stats = stats.get(name, {})
            evaluated = (
                agent_stats.get("directional_hits", 0) + agent_stats.get("directional_misses", 0)
            )
            reliability = round((1 - health.error_rate) * 100, 2)
            accuracy = (
                round(agent_stats["directional_hits"] / evaluated * 100, 2) if evaluated else None
            )
            avg_confidence = (
                round(agent_stats["confidence_sum"] / agent_stats["confidence_count"], 1)
                if agent_stats.get("confidence_count")
                else None
            )
            penalty = agent_stats.get("overconfident_losses", 0) * 5
            score = None
            if evaluated:
                score = round(max(0.0, (accuracy or 0) * 0.5 + reliability * 0.5 - penalty), 2)
            report.append(
                {
                    "agent_name": name,
                    "reliability_score": reliability,
                    "directional_accuracy": accuracy,
                    "avg_confidence": avg_confidence,
                    "signal_distribution": agent_stats.get("signals", {}),
                    "overconfident_losses": agent_stats.get("overconfident_losses", 0),
                    "evaluated_decisions": evaluated,
                    "score": score,
                    "sample_sufficient": evaluated >= MIN_SAMPLES_FOR_WEIGHT_ADJUSTMENT,
                    "avg_latency_ms": health.avg_latency_ms,
                    "total_runs": health.total_runs,
                    "total_failures": health.total_failures,
                    "note": "Report only — weights are not auto-adjusted (docs/27).",
                }
            )
        return report
