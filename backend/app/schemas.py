"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class WindowBuildPayload(BaseModel):
    batch_key: str
    grid_precision: int = Field(default=6, ge=1, le=12)
    bucket_sec: int = Field(default=3600, ge=1)
    expected_windows: list[dict[str, Any]] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=0)


class WindowClaimPayload(BaseModel):
    batch_key: str
    window_ids: list[str] | None = None
    scope: str = "all"


class WindowConfirmPayload(BaseModel):
    formal_version: int | None = Field(default=None, ge=1)
