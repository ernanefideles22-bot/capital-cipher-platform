"""Market Data Agent (docs/05 §1, docs/11).

Provides candles and connection status to the orchestrator. Uses public data
only in Phase 1 — never private API keys.
"""

from __future__ import annotations

from app.agents.base import BaseAgent
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal


class MarketDataAgent(BaseAgent):
    name = "MarketDataAgent"
    description = "Collects and serves normalized public market data"
    required_inputs = ("exchange", "symbol", "timeframe")
    capabilities = ("market-data-availability", "connection-health")
    decision_role = "PRIMARY"
    critical = True

    def __init__(self, store: CandleStore, connection_status_fn=None) -> None:
        super().__init__()
        self._store = store
        self._connection_status_fn = connection_status_fn or (lambda: "UNKNOWN")

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        exchange = agent_input.market_context.get("exchange", "BINANCE")
        candles = self._store.get(exchange, agent_input.symbol, agent_input.timeframe, limit=200)
        connection_status = self._connection_status_fn()
        if not candles:
            return self._output(
                AgentStatus.BLOCKED,
                Signal.BLOCK,
                0,
                "No market data available for the requested symbol/timeframe",
                evidence={"connection_status": connection_status, "candle_count": 0},
                warnings=["DATA_QUALITY_ISSUE"],
            )
        return self._output(
            AgentStatus.COMPLETED,
            Signal.NEUTRAL,
            100,
            "Market data available",
            evidence={
                "connection_status": connection_status,
                "candle_count": len(candles),
                "last_close": candles[-1].close,
                "last_closed_at": candles[-1].closed_at.isoformat(),
            },
        )
