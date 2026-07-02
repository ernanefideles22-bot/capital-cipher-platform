"""WebSocket streams (docs/13): /ws/system, /ws/market, /ws/logs, /ws/decisions."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.event_bus import Topics
from app.schemas.events import BusMessage

router = APIRouter()


class WsHub:
    """Fan-out hub bridging the event bus to websocket clients."""

    def __init__(self) -> None:
        self.channels: dict[str, set[WebSocket]] = {
            "system": set(),
            "market": set(),
            "logs": set(),
            "decisions": set(),
        }

    async def connect(self, channel: str, ws: WebSocket) -> None:
        await ws.accept()
        self.channels[channel].add(ws)

    def disconnect(self, channel: str, ws: WebSocket) -> None:
        self.channels[channel].discard(ws)

    async def broadcast(self, channel: str, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in self.channels.get(channel, set()):
            try:
                await ws.send_text(json.dumps(payload, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(channel, ws)


hub = WsHub()


def wire_bus_to_hub(event_bus) -> None:
    """Subscribe hub broadcasts to relevant bus topics."""

    async def market_handler(message: BusMessage) -> None:
        await hub.broadcast("market", message.model_dump(mode="json"))

    async def decision_handler(message: BusMessage) -> None:
        await hub.broadcast("decisions", message.model_dump(mode="json"))

    async def system_handler(message: BusMessage) -> None:
        await hub.broadcast("system", message.model_dump(mode="json"))

    async def log_handler(message: BusMessage) -> None:
        await hub.broadcast("logs", message.model_dump(mode="json"))

    event_bus.subscribe(Topics.MARKET_EVENTS, market_handler)
    event_bus.subscribe(Topics.DECISION_EVENTS, decision_handler)
    event_bus.subscribe(Topics.RISK_EVENTS, decision_handler)
    event_bus.subscribe(Topics.PAPER_ORDERS, decision_handler)
    event_bus.subscribe(Topics.SYSTEM_EVENTS, system_handler)
    event_bus.subscribe(Topics.AUDIT_EVENTS, log_handler)


async def _keepalive(channel: str, ws: WebSocket) -> None:
    try:
        while True:
            await asyncio.sleep(30)
            await ws.send_text(json.dumps({"type": "ping"}))
    except Exception:
        hub.disconnect(channel, ws)


@router.websocket("/ws/system")
async def ws_system(ws: WebSocket) -> None:
    await _serve("system", ws)


@router.websocket("/ws/market")
async def ws_market(ws: WebSocket) -> None:
    await _serve("market", ws)


@router.websocket("/ws/logs")
async def ws_logs(ws: WebSocket) -> None:
    await _serve("logs", ws)


@router.websocket("/ws/decisions")
async def ws_decisions(ws: WebSocket) -> None:
    await _serve("decisions", ws)


async def _serve(channel: str, ws: WebSocket) -> None:
    await hub.connect(channel, ws)
    try:
        while True:
            # Clients may send pings; we ignore content.
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(channel, ws)
