"""Opt-in SHADOW interface for Gate professional market reading.

Never self-registers in the deployed 300-agent roster. No OMS/risk calls and
no executable BUY/SELL signal; evidence is descriptive and timestamp-bounded.
"""
from __future__ import annotations

from app.agents.specialists import CandleSpecialistAgent
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal
from app.strategy.professional_gate_chart_stage1 import (
    MIN_BARS,analyze_gate_multi_timeframe,
)
from app.strategy.gate_futures_testnet_feed_stage1 import ALLOWED_CONTRACTS


class GateProfessionalChartShadowAgent(CandleSpecialistAgent):
    name="GateProfessionalChartShadowAgent"
    version="1.0.0"
    description="Causal multi-timeframe GATE price-action research, no trade authority"
    capabilities=("gate-price-action","confirmed-pivots","three-timeframe-confluence")
    decision_role="SHADOW"
    critical=False

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        if (agent_input.market_context.get("exchange")!="GATE"
                or agent_input.symbol not in ALLOWED_CONTRACTS):
            return self._output(
                AgentStatus.BLOCKED,Signal.WAIT,0,
                "Gate Futures TestNet chart research requires an allowed GATE market",
                warnings=["UNSUPPORTED_MARKET"],
            )
        by_tf={tf:self._store.get("GATE",agent_input.symbol,tf,limit=300)
               for tf in ("15m","1h","4h")}
        if any(len(bars)<MIN_BARS for bars in by_tf.values()):
            return self._output(
                AgentStatus.COMPLETED,Signal.WAIT,0,
                "Insufficient three-timeframe chart history",
                evidence={"counts":{tf:len(bars) for tf,bars in by_tf.items()},
                          "execution_authorized":False},
                warnings=["INSUFFICIENT_HISTORY"],
            )
        try:
            report=analyze_gate_multi_timeframe(
                by_tf,as_of=agent_input.timestamp,
            )
        except ValueError:
            return self._output(
                AgentStatus.COMPLETED,Signal.WAIT,0,
                "Inconsistent, stale or unavailable Gate chart history",
                warnings=["CAUSAL_DATA_QUALITY_BLOCKED"],
                evidence={"execution_authorized":False},
            )
        return self._output(
            AgentStatus.COMPLETED,Signal.NEUTRAL,0,
            "Research-only Gate market structure evaluated; no trading recommendation",
            evidence={
                "venue":"GATE_FUTURES_TESTNET_RESEARCH_ONLY",
                "symbol":agent_input.symbol,
                "observed_as_of_utc":report.as_of.isoformat(),
                "timeframe_alignment":report.alignment,
                "chart_readings":[{
                    "timeframe":r.timeframe,
                    "last_candle_close_utc":r.last_close_at.isoformat(),
                    "regime":r.regime,
                    "market_structure":r.market_structure,
                    "candle_patterns":list(r.candle_patterns),
                    "chart_patterns":list(r.chart_patterns),
                    "support":r.support,
                    "resistance":r.resistance,
                    "fibonacci_retracements":list(r.fibonacci_retracements),
                    "relative_volume":r.relative_volume,
                } for r in report.readings],
                "source_authenticated":False,
                "edge_proven":False,
                "execution_authorized":False,
            },
        )
