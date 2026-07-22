"""Redis Streams transport for versioned Capital Cipher events."""

from __future__ import annotations

import re
from typing import Any

from app.core.payload_security import ensure_payload_has_no_secrets
from app.core.transports.base import BrokerRecord
from app.schemas.events import BusMessage


class RedisStreamTransport:
    """Publish/read event envelopes through Redis Streams.

    Redis is an at-least-once transport here. Event IDs remain the
    idempotency boundary and PostgreSQL remains the durable audit source.
    """

    def __init__(
        self,
        redis_url: str,
        *,
        stream_prefix: str = "capital-cipher",
        max_stream_length: int = 100_000,
        max_message_bytes: int = 1_000_000,
        client: Any | None = None,
    ) -> None:
        if not redis_url.strip():
            raise ValueError("redis_url is required")
        if not re.fullmatch(r"[a-z0-9][a-z0-9:-]{1,63}", stream_prefix):
            raise ValueError("stream_prefix contains unsupported characters")
        if max_stream_length < 1 or max_message_bytes < 1:
            raise ValueError("Redis stream limits must be positive")
        if client is None:
            from redis.asyncio import Redis

            client = Redis.from_url(
                redis_url,
                decode_responses=True,
                health_check_interval=30,
            )
        self._client = client
        self._stream_prefix = stream_prefix
        self._max_stream_length = max_stream_length
        self._max_message_bytes = max_message_bytes

    def stream_key(self, topic: str) -> str:
        return f"{self._stream_prefix}:{topic}"

    async def healthcheck(self) -> bool:
        return bool(await self._client.ping())

    async def publish(self, message: BusMessage) -> str:
        ensure_payload_has_no_secrets(message.payload)
        encoded = message.model_dump_json()
        if len(encoded.encode("utf-8")) > self._max_message_bytes:
            raise ValueError("Broker message exceeds max_message_bytes")
        stream_id = await self._client.xadd(
            self.stream_key(message.topic),
            {
                "message": encoded,
                "event_id": message.event_id,
                "correlation_id": message.correlation_id,
                "event_type": message.event_type,
                "schema_version": message.schema_version,
            },
            maxlen=self._max_stream_length,
            approximate=True,
        )
        return self._decode(stream_id)

    async def read_after(
        self,
        topic: str,
        *,
        after_id: str = "0-0",
        count: int = 100,
        block_ms: int = 0,
    ) -> list[BrokerRecord]:
        if count < 1 or count > 10_000:
            raise ValueError("count must be between 1 and 10000")
        if block_ms < 0 or block_ms > 60_000:
            raise ValueError("block_ms must be between 0 and 60000")
        response = await self._client.xread(
            {self.stream_key(topic): after_id},
            count=count,
            block=block_ms or None,
        )
        records: list[BrokerRecord] = []
        for _, entries in response:
            for stream_id, fields in entries:
                raw_message = fields.get("message") or fields.get(b"message")
                if raw_message is None:
                    raise ValueError("Redis stream entry is missing the message field")
                records.append(
                    BrokerRecord(
                        stream_id=self._decode(stream_id),
                        message=BusMessage.model_validate_json(self._decode(raw_message)),
                    )
                )
        return records

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _decode(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
