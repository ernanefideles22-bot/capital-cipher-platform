"""Standard API response envelope (docs/13-api-specification.md)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.common import utcnow


class ApiMeta(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=utcnow)


class ApiError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ApiResponse(BaseModel):
    success: bool
    data: Any = None
    error: ApiError | None = None
    meta: ApiMeta = Field(default_factory=ApiMeta)


def success_response(data: Any) -> dict:
    return ApiResponse(success=True, data=data).model_dump(mode="json")


def error_response(code: str, message: str, details: dict | None = None) -> dict:
    return ApiResponse(
        success=False, error=ApiError(code=code, message=message, details=details or {})
    ).model_dump(mode="json")
