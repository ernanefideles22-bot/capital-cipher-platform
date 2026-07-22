"""Version-aware registry and lifecycle governance for runtime agents."""

from __future__ import annotations

from collections import OrderedDict

from app.agents.base import BaseAgent
from app.core.errors import ConfigurationError, ValidationError
from app.schemas.agents import AgentRegistration


class AgentRegistry:
    """Own active agent instances without allowing implicit replacement."""

    def __init__(self, agents: list[BaseAgent] | None = None) -> None:
        self._agents: OrderedDict[str, BaseAgent] = OrderedDict()
        self._history: list[AgentRegistration] = []
        for agent in agents or []:
            self.register(agent)

    @property
    def agents(self) -> dict[str, BaseAgent]:
        return dict(self._agents)

    @property
    def history(self) -> list[AgentRegistration]:
        return list(self._history)

    def register(self, agent: BaseAgent) -> AgentRegistration:
        registration = agent.registration()
        if registration.execution_mode != "PAPER":
            raise ConfigurationError("Only PAPER agents may enter the registry")
        if registration.agent_name in self._agents:
            raise ConfigurationError(
                f"Agent {registration.agent_name} is already registered"
            )
        self._agents[registration.agent_name] = agent
        return registration

    def get(
        self,
        agent_name: str,
        *,
        version: str | None = None,
    ) -> BaseAgent:
        agent = self._agents.get(agent_name)
        if agent is None:
            raise ValidationError(f"Agent {agent_name} is not registered")
        if version is not None and agent.version != version:
            raise ValidationError(
                f"Agent {agent_name} version {version} is not active"
            )
        return agent

    def registrations(self) -> list[AgentRegistration]:
        return [
            agent.registration()
            for agent in self._agents.values()
        ]

    def decision_agents(self) -> list[BaseAgent]:
        return [
            agent
            for agent in self._agents.values()
            if agent.registration().decision_role == "PRIMARY"
        ]

    def shadow_agents(self) -> list[BaseAgent]:
        return [
            agent
            for agent in self._agents.values()
            if agent.registration().decision_role == "SHADOW"
        ]

    async def initialize_all(self) -> None:
        for agent in self._agents.values():
            await agent.initialize()

    def disable(self, agent_name: str) -> AgentRegistration:
        agent = self.get(agent_name)
        agent.disable()
        return agent.registration()

    def enable(self, agent_name: str) -> AgentRegistration:
        agent = self.get(agent_name)
        agent.enable()
        return agent.registration()

    def replace(
        self,
        agent: BaseAgent,
        *,
        expected_version: str,
    ) -> AgentRegistration:
        current = self.get(agent.name)
        if current.version != expected_version:
            raise ValidationError(
                f"Agent {agent.name} active version changed before replacement"
            )
        if current.version == agent.version:
            raise ValidationError("Replacement requires a new agent version")
        current.disable()
        self._history.append(current.registration())
        self._agents[agent.name] = agent
        return agent.registration()

    def remove(self, agent_name: str) -> AgentRegistration:
        agent = self.get(agent_name)
        agent.disable()
        agent.status = "REMOVED"
        registration = agent.registration()
        self._history.append(registration)
        del self._agents[agent_name]
        return registration

    def validate_cohort(self, *, expected_count: int) -> None:
        registrations = self.registrations()
        if len(registrations) != expected_count:
            raise ConfigurationError(
                f"Expected {expected_count} PAPER agents, "
                f"found {len(registrations)}"
            )
        names = [registration.agent_name for registration in registrations]
        if len(names) != len(set(names)):
            raise ConfigurationError("Agent registry contains duplicate names")
        for registration in registrations:
            if not registration.capabilities:
                raise ConfigurationError(
                    f"Agent {registration.agent_name} has no capability"
                )
