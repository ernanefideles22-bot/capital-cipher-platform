"""HTTP security controls for the internal paper-trading API."""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.schemas.api import error_response


class InMemoryRateLimiter:
    """Fixed-window request limiter for the single-process Phase 1 runtime.

    This is intentionally local to one process. A shared Redis-backed limiter
    is required before horizontal scaling, but this boundary prevents an
    unbounded public API during Phase 1.
    """

    def __init__(self, max_requests: int, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            bucket = self._requests[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                retry_after = max(1, math.ceil(bucket[0] + self.window_seconds - now))
                return False, retry_after
            bucket.append(now)
            return True, 0


class ApiSecurityMiddleware:
    """Apply request limits, body limits, and baseline response headers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        requests_per_minute: int,
        max_request_body_bytes: int,
    ) -> None:
        self.app = app
        self._limiter = InMemoryRateLimiter(requests_per_minute)
        self._max_request_body_bytes = max_request_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        if method != "OPTIONS" and path != "/health":
            client_info = scope.get("client")
            client = client_info[0] if client_info else "unknown"
            allowed, retry_after = await self._limiter.allow(client)
            if not allowed:
                await self._send_error(
                    scope,
                    receive,
                    send,
                    429,
                    "RATE_LIMITED",
                    "Too many requests",
                    headers={"Retry-After": str(retry_after)},
                )
                return

        content_length = Headers(scope=scope).get("content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > self._max_request_body_bytes
            except ValueError:
                await self._send_error(
                    scope,
                    receive,
                    send,
                    400,
                    "INVALID_CONTENT_LENGTH",
                    "Invalid Content-Length header",
                )
                return
            if too_large:
                await self._send_error(
                    scope, receive, send, 413, "REQUEST_TOO_LARGE", "Request body exceeds the limit"
                )
                return

        replay_receive = receive
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            received_bytes = 0
            messages: deque[Message] = deque()
            while True:
                message = await receive()
                messages.append(message)
                if message["type"] == "http.disconnect":
                    break
                if message["type"] != "http.request":
                    continue
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._max_request_body_bytes:
                    await self._send_error(
                        scope,
                        receive,
                        send,
                        413,
                        "REQUEST_TOO_LARGE",
                        "Request body exceeds the limit",
                    )
                    return
                if not message.get("more_body", False):
                    break

            async def buffered_receive() -> Message:
                if messages:
                    return messages.popleft()
                return {"type": "http.request", "body": b"", "more_body": False}

            replay_receive = buffered_receive

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            await send(message)

        await self.app(scope, replay_receive, secure_send)

    @staticmethod
    async def _send_error(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content=error_response(code, message),
            headers=headers,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        await response(scope, receive, send)
