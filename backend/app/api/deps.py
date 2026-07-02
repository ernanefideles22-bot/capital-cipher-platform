"""API dependencies: context access and authentication (docs/13, docs/16)."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from app.api.context import AppContext


def get_context(request: Request) -> AppContext:
    return request.app.state.context


async def require_admin(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> None:
    """Sensitive endpoints must require authentication (docs/16).

    Fail-safe: if no ADMIN_API_KEY is configured, sensitive endpoints are
    denied rather than left open.
    """
    context: AppContext = request.app.state.context
    configured = context.settings.admin_api_key
    if not configured:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN",
                "message": "ADMIN_API_KEY not configured; sensitive endpoints are locked",
            },
        )
    if x_api_key != configured:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "Invalid or missing API key"},
        )


AdminRequired = Depends(require_admin)
