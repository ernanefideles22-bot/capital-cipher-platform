"""Executable source audit and conservative department projection.

Inspects the actual registry without initializing, running, disabling, moving or
promoting an agent. Requested staffing targets are never filled by inventing
capabilities. Managers are proposed logical roles, not extra registered agents.
"""

from __future__ import annotations

import hashlib
import inspect
from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path

from app.agents.base import BaseAgent
from app.agents.registry import AgentRegistry
from app.office.contracts import Department as D
from app.schemas.agents import AgentHealth, AgentRegistration


@dataclass(frozen=True)
class DepartmentDefinition:
    department: D
    label: str
    target: int
    responsibility: str
    metric: str


DEPARTMENTS = (
    DepartmentDefinition(D.MARKET, "Inteligência de mercado", 40, "Qualidade e contexto de mercado", "freshness, coverage"),
    DepartmentDefinition(D.TECHNICAL, "Análise técnica avançada", 40, "OHLCV e confluências verificáveis", "coverage, out-of-sample precision"),
    DepartmentDefinition(D.QUANT, "Pesquisa quantitativa", 35, "Hipóteses e testes reproduzíveis", "out-of-sample stability, calibration"),
    DepartmentDefinition(D.STRATEGY, "Estratégias de trading", 35, "Propostas versionadas para as três mesas", "net expectancy, drawdown"),
    DepartmentDefinition(D.EXECUTION, "Microestrutura e execução", 30, "Contexto de liquidez para o OMS central", "slippage, latency p50/p95/p99"),
    DepartmentDefinition(D.CRYPTO, "Inteligência cripto", 25, "Evidências de derivativos e on-chain", "freshness, provider coverage"),
    DepartmentDefinition(D.MACRO, "Macroeconomia e notícias", 20, "Contexto externo com proveniência", "freshness, source quality"),
    DepartmentDefinition(D.PORTFOLIO, "Gestão de portfólio", 25, "Propostas de alocação sem autoridade de risco", "gross/net exposure, concentration"),
    DepartmentDefinition(D.RISK, "Risco e segurança", 25, "Diagnósticos para o motor de risco independente", "blocked violations, drift"),
    DepartmentDefinition(D.LEARNING, "Aprendizado e auditoria", 25, "Avaliação SHADOW sem autopromoção", "Brier loss, drift, reproducibility"),
)

# Explicit semantic assignments for the twelve initial specialists. This is a
# proposed reporting affiliation; it does not change their runtime SHADOW role.
INITIAL = {
    "MomentumAgent": D.TECHNICAL, "VolatilityAgent": D.RISK,
    "VolumeAgent": D.MARKET, "VWAPAgent": D.TECHNICAL,
    "MACDAgent": D.TECHNICAL, "EMAAlignmentAgent": D.TECHNICAL,
    "MeanReversionAgent": D.STRATEGY, "BreakoutAgent": D.STRATEGY,
    "SupportResistanceAgent": D.TECHNICAL, "CandleStructureAgent": D.TECHNICAL,
    "LiquidityProxyAgent": D.EXECUTION, "DataQualityAgent": D.RISK,
}

ADVANCED = {
    "ReturnDistributionAgent": D.QUANT, "DownsideVolatilityAgent": D.RISK,
    "UpsideCaptureAgent": D.PORTFOLIO, "DrawdownAgent": D.PORTFOLIO,
    "TrendStrengthAgent": D.TECHNICAL, "EfficiencyRatioAgent": D.QUANT,
    "AutocorrelationAgent": D.QUANT, "SkewProxyAgent": D.QUANT,
    "TailRiskAgent": D.RISK, "GapAgent": D.MARKET,
    "RangeExpansionAgent": D.TECHNICAL, "CompressionAgent": D.TECHNICAL,
    "VolumeTrendAgent": D.MARKET, "VolumeVolatilityAgent": D.MARKET,
    "PriceVolumeCorrelationAgent": D.QUANT, "MoneyFlowProxyAgent": D.EXECUTION,
    "CloseLocationAgent": D.TECHNICAL, "WickImbalanceAgent": D.TECHNICAL,
    "ConsecutiveRunsAgent": D.TECHNICAL, "RegimePersistenceAgent": D.MARKET,
    "FractalDimensionProxyAgent": D.QUANT, "EntropyAgent": D.QUANT,
    "ShockRecoveryAgent": D.RISK, "DataFreshnessAgent": D.RISK,
    "CrossWindowConsensusAgent": D.TECHNICAL,
}

MONTH9 = {
    "RETURN": D.QUANT, "VOLATILITY": D.RISK, "DRAWDOWN": D.PORTFOLIO,
    "VOLUME_PRESSURE": D.MARKET, "RANGE_EFFICIENCY": D.QUANT,
    "TAIL_BALANCE": D.RISK, "AUTOCORRELATION": D.QUANT,
    "LIQUIDITY": D.EXECUTION,
}
MONTH10 = {
    "ENTROPY": D.QUANT, "JUMP_INTENSITY": D.RISK,
    "VOLUME_ANOMALY": D.MARKET, "GAP_PRESSURE": D.MARKET,
    "TREND_PERSISTENCE": D.MARKET, "VOL_OF_VOL": D.RISK,
    "CLOSE_LOCATION": D.TECHNICAL, "WICK_IMBALANCE": D.TECHNICAL,
    "DOWNSIDE_CAPTURE": D.PORTFOLIO,
}


def _affiliation(agent: BaseAgent) -> tuple[D, str]:
    module = type(agent).__module__
    definition = getattr(agent, "_definition", None)
    if module == "app.agents.market_data":
        return D.MARKET, "Existing market availability gate"
    if module in {"app.agents.quant", "app.agents.trend"}:
        return D.TECHNICAL, "Existing PRIMARY technical/regime logic retained"
    if module == "app.agents.specialists" and agent.name in INITIAL:
        return INITIAL[agent.name], "Existing named OHLCV specialist"
    if module == "app.agents.advanced_specialists" and agent.name in ADVANCED:
        return ADVANCED[agent.name], "Existing single-metric OHLCV diagnostic"
    if module == "app.agents.month8_specialists":
        domain = getattr(definition, "domain", None)
        if domain in {"DERIVATIVES", "ONCHAIN"}:
            return D.CRYPTO, f"Governed {domain} evidence; availability must be measured"
        if domain in {"MACRO", "NEWS"}:
            return D.MACRO, f"Governed {domain} evidence; no collector assumed"
        if type(agent).__name__ == "Month8TechnicalSpecialist":
            return D.TECHNICAL, "Existing technical formula, including explicit proxies"
    if module == "app.agents.month9_specialists" and definition.family in MONTH9:
        return MONTH9[definition.family], f"Existing {definition.family} diagnostic window"
    if module == "app.agents.month10_specialists" and definition.family in MONTH10:
        return MONTH10[definition.family], f"Existing {definition.family} resilience diagnostic"
    if module == "app.agents.month11_specialists":
        return D.LEARNING, "Existing shadow validation diagnostic; no training/autonomy inferred"
    raise ValueError(f"Unreviewed agent implementation: {agent.name} ({module})")


PUBLIC_DERIVATIVES_METRICS = {
    "funding_rate", "open_interest_change", "basis",
    "long_short_ratio", "taker_buy_sell_ratio",
}


def _source(agent: BaseAgent) -> dict:
    source = Path(inspect.getfile(type(agent)))
    app_root = Path(__file__).resolve().parents[1]
    return {
        "class": f"{type(agent).__module__}.{type(agent).__qualname__}",
        "path": "backend/app/" + source.resolve().relative_to(app_root).as_posix(),
        "line": inspect.getsourcelines(type(agent))[1],
        "file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


def build_inventory(
    registry: AgentRegistry,
    *,
    observed_registrations: list[AgentRegistration] | None = None,
    observed_health: list[AgentHealth] | None = None,
) -> dict:
    """Snapshot a complete cohort. Reject drift and never fabricate live metrics."""
    registry.validate_cohort(expected_count=300)
    registrations = registry.registrations()
    local = {item.agent_name: item for item in registrations}
    health = {item.name: item for item in observed_health or []}
    if observed_registrations is not None:
        observed = {item.agent_name: item for item in observed_registrations}
        if len(observed) != len(observed_registrations) or observed != local:
            raise ValueError("Observed registration cohort differs from audited registry")
    if observed_health is not None:
        if len(health) != len(observed_health) or set(health) != set(local):
            raise ValueError("Observed health must cover the complete unique cohort")
        if any(health[name].version != item.version for name, item in local.items()):
            raise ValueError("Observed health versions differ from audited registry")
    rows = []
    for agent in registry.agents.values():
        registration = local[agent.name]
        department, reason = _affiliation(agent)
        definition = getattr(agent, "_definition", None)
        domain = getattr(definition, "domain", None)
        metric = getattr(definition, "metric_name", None)
        external = domain is not None
        supplied = health.get(agent.name)
        state = supplied.model_dump(mode="json") if supplied else None
        rows.append({
            "registration": registration.model_dump(mode="json"),
            "implementation": _source(agent),
            "definition_parameters": asdict(definition) if is_dataclass(definition) else {
                "metric_index": getattr(agent, "_metric_index", None),
            },
            "proposed_department": department.value,
            "affiliation_reason": reason,
            "operational_assignment_changed": False,
            "model_kind": "DETERMINISTIC_THRESHOLD" if external else "DETERMINISTIC_OHLCV",
            "llm_dependency": False,
            "source_kind": "GOVERNED_EXTERNAL_EVIDENCE" if external else "NORMALIZED_OHLCV_STORE",
            "external_domain": domain,
            "collector_implemented": (
                metric in PUBLIC_DERIVATIVES_METRICS and domain == "DERIVATIVES"
            ) if external else None,
            "live_source_coverage": None,
            "dependencies": ["SpecialistEvidenceService", "Repository"] if external else ["CandleStore"],
            "communication": "AgentInput/AgentOutput via AgentRuntime; no direct peer calls",
            "strategy_binding": "Shared StrategyEngine by symbol/timeframe; no per-agent strategy instance",
            "decision_participation": (
                "BASELINE_PRIMARY_GATE" if registration.decision_role == "PRIMARY"
                else "SHADOW_EVIDENCE; eligible confirmation may veto, never originate an order"
            ),
            "permissions": {"submit_order": False, "approve_risk": False, "promote_strategy": False},
            "permission_enforcement": "Architectural dependency boundary, not an OS/process sandbox",
            "memory": {
                "process": "last input/output and health counters",
                "durable": "runtime jobs, attempts, input/output memory when Repository is configured",
                "adaptive_learning_implemented": False,
            },
            "parallelism": "asyncio cooperative tasks, bounded runtime/worker semaphores; not 300 OS workers",
            "health_observation": state,
            "latency_ms": {
                "mean": supplied.avg_latency_ms if supplied and supplied.total_runs else None,
                "p50": None, "p95": None, "p99": None,
                "analysis_timeout": agent.timeout_ms,
                "prefetch_timeout": agent.evidence_prefetch_timeout_ms,
            },
        })
    counts = Counter(row["proposed_department"] for row in rows)
    departments = [{
        **asdict(item),
        "department": item.department.value,
        "proposed_manager": f"manager:{item.department.value.lower()}",
        "manager_status": "DESIGN_ONLY",
        "manager_is_additional_agent": False,
        "existing_affiliations": counts[item.department.value],
        "target_delta": item.target - counts[item.department.value],
        "communication_contract": "office-analysis-v1",
        "proposed_permissions": ["READ_CONTEXT", "PUBLISH_ANALYSIS", "PROPOSE_EXPERIMENT"],
        "operational_authority": False,
    } for item in DEPARTMENTS]
    return {
        "schema_version": "office-inventory-v1",
        "mode": "READ_ONLY_AUDIT",
        "agent_count": len(rows),
        "decision_roles": dict(Counter(item.decision_role for item in registrations)),
        "runtime_health_observed": observed_health is not None,
        "observed_status_counts": dict(Counter(item.status for item in health.values())) if observed_health is not None else None,
        "requested_distribution_compatible_without_retraining": all(item["target_delta"] == 0 for item in departments),
        "executive_board": {"status": "DESIGN_ONLY", "order_authority": False, "promotion_authority": False},
        "departments": departments,
        "agents": rows,
        "green_coff": {"status": "RESERVED_UNDOCUMENTED", "enabled": False},
    }
