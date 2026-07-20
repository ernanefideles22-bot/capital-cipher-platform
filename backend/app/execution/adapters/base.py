"""Fail-closed contract shared by TESTNET execution adapters."""

from __future__ import annotations

import abc

from app.schemas.common import Exchange
from app.schemas.oms import (
    ExecutionEnvironment,
    OMSOrder,
    VenueOrderSnapshot,
    VenueStateSnapshot,
)


class ExchangeExecutionAdapter(abc.ABC):
    exchange: Exchange
    environment = ExecutionEnvironment.TESTNET

    @abc.abstractmethod
    async def healthcheck(self) -> bool:
        raise NotImplementedError

    async def prepare_order(self, order: OMSOrder) -> OMSOrder:
        """Apply venue constraints before durable approval consumption."""

        return order

    @abc.abstractmethod
    async def submit_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        raise NotImplementedError

    @abc.abstractmethod
    async def cancel_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        raise NotImplementedError

    @abc.abstractmethod
    async def fetch_state(
        self,
        *,
        symbols: set[str] | None = None,
    ) -> VenueStateSnapshot:
        raise NotImplementedError

    @abc.abstractmethod
    async def aclose(self) -> None:
        raise NotImplementedError
