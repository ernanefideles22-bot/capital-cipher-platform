"""Real Redis Streams integration test, enabled by REDIS_TEST_URL."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.core.event_bus import Topics
from app.core.transports.redis_streams import RedisStreamTransport
from app.schemas.events import BusMessage


@pytest.mark.skipif(
    not os.environ.get("REDIS_TEST_URL"),
    reason="REDIS_TEST_URL is not configured",
)
async def test_real_redis_stream_publish_and_replay():
    transport = RedisStreamTransport(
        os.environ["REDIS_TEST_URL"],
        stream_prefix=f"capital-cipher-ci-{uuid4().hex[:8]}",
        max_stream_length=100,
    )
    message = BusMessage(
        event_id=str(uuid4()),
        correlation_id=str(uuid4()),
        topic=Topics.SYSTEM_EVENTS,
        event_type="SYSTEM_STARTED",
        source="redis-integration-test",
        payload={"mode": "PAPER"},
    )

    assert await transport.healthcheck() is True
    stream_id = await transport.publish(message)
    records = await transport.read_after(Topics.SYSTEM_EVENTS, after_id="0-0")
    await transport.close()

    assert records[-1].stream_id == stream_id
    assert records[-1].message == message
