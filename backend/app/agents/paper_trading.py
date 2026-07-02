"""Paper Trading Agent (docs/05 §5): wraps the PaperTradingEngine."""

from __future__ import annotations

from app.agents.base import BaseAgent
from app.paper_trading.engine import PaperTradingEngine
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal
from app.schemas.decisions import Decision
from app.schemas.risk import RiskCheck


class PaperTradingAgent(BaseAgent):
    name = "PaperTradingAgent"
    description = "Simulates order execution with fees and slippage"
    critical = False

    def __init__(self, engine: PaperTradingEngine) -> None:
        super().__init__()
        self._engine = engine

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        ctx = agent_input.market_context
        decision = Decision(**ctx["decision"])
        risk_check = RiskCheck(**ctx["risk_check"])
        order = await self._engine.create_order(
            decision, risk_check, current_price=float(ctx["current_price"])
        )
        return self._output(
            AgentStatus.COMPLETED,
            Signal.NEUTRAL,
            100,
            f"Paper order {order.status.value}",
            evidence={"paper_order": order.model_dump(mode="json")},
        )
