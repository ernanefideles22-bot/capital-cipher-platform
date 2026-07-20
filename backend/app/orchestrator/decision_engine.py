"""Decision Engine (docs/25-decision-engine.md).

Consolidates agent outputs into a candidate decision using hierarchy and
weights — never simple voting (docs/03 anti-pattern). Strong conflicts become
WAIT/BLOCK, not a neutral average.

Initial weights (docs/25):
  Quant Agent            40%
  Trend Agent            30%
  Market Data Quality    20%
  Operational Context    10%
"""

from __future__ import annotations

from app.schemas.agents import AgentOutput
from app.schemas.common import AgentStatus, CandidateAction, Signal
from app.schemas.decisions import Decision

WEIGHTS = {"QuantAgent": 0.40, "TrendAgent": 0.30, "data_quality": 0.20, "context": 0.10}

CRITICAL_AGENTS = {"MarketDataAgent", "QuantAgent", "TrendAgent"}

DIRECTIONAL = {Signal.BUY: 1, Signal.SELL: -1}


class DecisionEngine:
    def __init__(self, *, minimum_candidate_confidence: int = 70, strategy: str = "SCALP_15M") -> None:
        self.minimum_confidence = minimum_candidate_confidence
        self.strategy = strategy

    def consolidate(
        self,
        *,
        correlation_id: str,
        symbol: str,
        timeframe: str,
        agent_outputs: list[AgentOutput],
        data_quality_score: int = 100,
        operational_context_score: int = 100,
        strategy: str | None = None,
        minimum_confidence: int | None = None,
    ) -> Decision:
        warnings: list[str] = []
        strategy_name = strategy or self.strategy
        min_conf = minimum_confidence if minimum_confidence is not None else self.minimum_confidence
        outputs_by_name = {o.agent_name: o for o in agent_outputs}
        agent_summary = [
            {
                "name": o.agent_name,
                "status": o.status.value,
                "signal": o.signal.value,
                "confidence": o.confidence,
                "reason": o.reason,
            }
            for o in agent_outputs
        ]

        def blocked(reason: str, extra_warnings: list[str] | None = None) -> Decision:
            return Decision(
                correlation_id=correlation_id,
                symbol=symbol,
                timeframe=timeframe,
                candidate_action=CandidateAction.BLOCK,
                confidence=0,
                strategy=strategy_name,
                reason=reason,
                agent_summary=agent_summary,
                warnings=warnings + (extra_warnings or []),
            )

        # 1-3. Validate inputs, drop invalid outputs, check critical agents.
        valid_outputs = [o for o in agent_outputs if o.status == AgentStatus.COMPLETED]
        missing_critical = [
            name
            for name in CRITICAL_AGENTS
            if name not in outputs_by_name
            or outputs_by_name[name].status != AgentStatus.COMPLETED
        ]
        # Shadow agents are evidence-only in Month 5. Their status, signal,
        # confidence, and warnings remain visible in agent_summary but cannot
        # affect operational confidence or decision authority.
        for output in agent_outputs:
            if output.agent_name not in CRITICAL_AGENTS:
                continue
            if output.status == AgentStatus.TIMEOUT:
                warnings.append("AGENT_TIMEOUT")
            warnings.extend(output.warnings)
        if missing_critical:
            return blocked(f"Critical agents unavailable: {', '.join(sorted(missing_critical))}")

        # Explicit BLOCK from any critical agent is respected.
        for output in valid_outputs:
            if output.agent_name in CRITICAL_AGENTS and output.signal == Signal.BLOCK:
                return blocked(f"{output.agent_name} blocked: {output.reason}")

        # 4. Data quality gate (docs/32: score < 60 blocks).
        if data_quality_score < 60:
            return blocked(
                f"Data quality score {data_quality_score} below minimum",
                ["DATA_QUALITY_ISSUE"],
            )
        if data_quality_score < 80:
            warnings.append("DATA_QUALITY_ISSUE")

        # 5-6. Consolidate directional signals with weights.
        quant = outputs_by_name.get("QuantAgent")
        trend = outputs_by_name.get("TrendAgent")

        # Strong conflict rule (docs/25): opposing high-confidence signals -> WAIT.
        if (
            quant is not None
            and trend is not None
            and quant.signal in DIRECTIONAL
            and trend.signal in DIRECTIONAL
            and DIRECTIONAL[quant.signal] != DIRECTIONAL[trend.signal]
            and quant.confidence >= 70
            and trend.confidence >= 70
        ):
            warnings.append("CONFLICTING_SIGNALS")
            return Decision(
                correlation_id=correlation_id,
                symbol=symbol,
                timeframe=timeframe,
                candidate_action=CandidateAction.WAIT,
                confidence=0,
                strategy=strategy_name,
                reason=(
                    f"Strong conflict: QuantAgent {quant.signal.value} ({quant.confidence}) vs "
                    f"TrendAgent {trend.signal.value} ({trend.confidence})"
                ),
                agent_summary=agent_summary,
                warnings=warnings,
            )

        weighted_direction = 0.0
        for output, weight_key in ((quant, "QuantAgent"), (trend, "TrendAgent")):
            if output is not None and output.signal in DIRECTIONAL:
                weighted_direction += (
                    DIRECTIONAL[output.signal] * (output.confidence / 100) * WEIGHTS[weight_key]
                )
        quality_factor = (data_quality_score / 100) * WEIGHTS["data_quality"]
        context_factor = (operational_context_score / 100) * WEIGHTS["context"]

        # 7. Confidence: directional strength scaled by quality/context.
        directional_strength = abs(weighted_direction) / (
            WEIGHTS["QuantAgent"] + WEIGHTS["TrendAgent"]
        )
        confidence = int(
            round(directional_strength * 100 * (0.7 + quality_factor + context_factor))
        )
        confidence = max(0, min(100, confidence - 5 * len(set(warnings))))

        if weighted_direction > 0.15:
            action = CandidateAction.BUY
        elif weighted_direction < -0.15:
            action = CandidateAction.SELL
        else:
            action = CandidateAction.HOLD
            confidence = min(confidence, 50)

        # 8. Minimum confidence rule.
        if action in (CandidateAction.BUY, CandidateAction.SELL) and confidence < min_conf:
            warnings.append("LOW_CONFIDENCE")
            return Decision(
                correlation_id=correlation_id,
                symbol=symbol,
                timeframe=timeframe,
                candidate_action=CandidateAction.WAIT,
                confidence=confidence,
                strategy=strategy_name,
                reason=(
                    f"Confidence {confidence} below minimum {min_conf} "
                    f"for {action.value}"
                ),
                agent_summary=agent_summary,
                warnings=warnings,
            )

        reasons = []
        if quant is not None:
            reasons.append(f"Quant: {quant.reason}")
        if trend is not None:
            reasons.append(f"Trend: {trend.reason}")
        return Decision(
            correlation_id=correlation_id,
            symbol=symbol,
            timeframe=timeframe,
            candidate_action=action,
            confidence=confidence,
            strategy=strategy_name,
            reason=" | ".join(reasons) or "Consolidated decision",
            agent_summary=agent_summary,
            warnings=sorted(set(warnings)),
        )
