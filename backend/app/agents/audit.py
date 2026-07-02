"""Audit Agent (docs/05 §6): records absolutely everything.

If auditing fails, the operation must be blocked (docs/10, docs/31).
"""

from __future__ import annotations

from app.agents.base import BaseAgent
from app.audit.service import AuditService
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal


class AuditAgent(BaseAgent):
    name = "AuditAgent"
    description = "Persists the full decision chain for auditability"
    critical = True

    def __init__(self, audit_service: AuditService) -> None:
        super().__init__()
        self._audit = audit_service

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        payload = agent_input.market_context.get("audit_payload", {})
        audit_type = agent_input.market_context.get("audit_type", "DECISION_CHAIN")
        entity_type = agent_input.market_context.get("entity_type", "decision")
        entity_id = agent_input.market_context.get("entity_id")
        record = await self._audit.record(
            correlation_id=agent_input.correlation_id,
            audit_type=audit_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
        )
        return self._output(
            AgentStatus.COMPLETED,
            Signal.NEUTRAL,
            100,
            "Decision chain stored successfully",
            evidence={"audit_id": record["audit_id"], "stored": True},
        )
