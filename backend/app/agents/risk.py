"""Risk Agent (docs/05 §4): wraps the RiskManager as an agent."""

from __future__ import annotations

from app.agents.base import BaseAgent
from app.risk.manager import RiskManager
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, RiskStatus, Signal
from app.schemas.decisions import Decision


class RiskAgent(BaseAgent):
    name = "RiskAgent"
    description = "Validates candidate decisions against risk limits"
    critical = True

    def __init__(self, risk_manager: RiskManager) -> None:
        super().__init__()
        self._risk = risk_manager
        self.last_check = None

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        ctx = agent_input.market_context
        decision = Decision(**ctx["decision"])
        check = await self._risk.check(
            decision,
            entry_price=float(ctx["entry_price"]),
            atr=ctx.get("atr"),
            data_quality_score=int(ctx.get("data_quality_score", 100)),
            market_data_delay_ms=int(ctx.get("market_data_delay_ms", 0)),
            balance=ctx.get("balance"),
        )
        self.last_check = check
        signal = Signal.BLOCK if not check.approved else Signal.NEUTRAL
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            100 if check.approved else 0,
            check.reason,
            evidence={"risk_check": check.model_dump(mode="json")},
            warnings=list(check.warnings),
        )
