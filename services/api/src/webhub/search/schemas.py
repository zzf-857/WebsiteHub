"""Request and response models for semantic index management."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SemanticIndexStatusResponse(BaseModel):
    """What the user sees before deciding to spend their own quota."""

    model_config = ConfigDict(extra="forbid")

    #: 未配 embedding Provider 时为 false，其余字段仍如实填写。
    configured: bool
    model_name: str | None
    total_sites: int
    indexed: int
    pending: int
    #: pending 是否被单轮上限截断；true 时界面要说「本轮」而不是「全部」。
    pending_capped: bool
    #: **花钱数字。** 触发回填前必须让用户看到这个数。
    estimated_requests: int
    #: 全库全部重建的总请求预估，不得用当前 pending 的预估代替。
    rebuild_estimated_requests: int
    #: 全部重建第一轮实际处理的站点数和请求数。
    rebuild_pass_sites: int
    rebuild_pass_estimated_requests: int
    #: 全库超过单轮上限时为 true；前端必须明确还需要后续补齐。
    rebuild_capped: bool
    #: 单轮站点上限，由服务端提供给确认界面。
    pass_limit: int
    #: 该账号是否已有一轮回填在跑。
    running: bool


class SemanticIndexRebuildRequest(BaseModel):
    """Rebuild drops the account's vectors first, so it is opt-in per request."""

    model_config = ConfigDict(extra="forbid")

    #: 必须显式传 true 才会丢弃现有向量重建。默认只补缺失的部分。
    drop_existing: bool = False
    #: 本轮最多处理多少个站点，用于把大库拆成几轮。
    limit: int = Field(default=512, ge=1, le=512)


class SemanticIndexRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 是否真的排了一轮新的。已有一轮在跑时为 false —— 「已在进行中」和
    #: 「已排队」是两个答案，混为一谈会让用户以为自己排了两轮。
    scheduled: bool
    #: scheduled=false 可能是任务占用，也可能是 Provider 不可用，调用方
    #: 必须按这个稳定代码区分，不能猜测。
    reason: Literal["scheduled", "already_running", "provider_unavailable"]
    #: 本次丢弃了多少条旧向量（`drop_existing` 为 false 时恒为 0）。
    dropped: int
    #: 排队时的预估请求数，便于前端把花钱数字回显给用户。
    estimated_requests: int


__all__ = [
    "SemanticIndexRebuildRequest",
    "SemanticIndexRunResponse",
    "SemanticIndexStatusResponse",
]
