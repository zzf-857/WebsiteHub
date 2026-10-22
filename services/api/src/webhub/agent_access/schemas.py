"""Website REST models for /settings/agents. Requests forbid extra fields."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AgentScope = Literal["read", "write"]


class AgentTokenCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    # ``["read"]`` 只读；含 ``"write"`` 即读写（write 隐含 read）。
    scopes: list[AgentScope] = Field(min_length=1, max_length=2)


class AgentTokenResponse(BaseModel):
    id: str
    name: str
    # 仅展示用的前缀，例如 ``whk_AbCd``；完整令牌与哈希都不出现在列表里。
    prefix: str
    scopes: list[AgentScope]
    created_at: datetime
    last_used_at: datetime | None


class AgentTokenCreatedResponse(AgentTokenResponse):
    # 明文令牌：只在创建响应里出现这一次。
    token: str


class AgentTokenListResponse(BaseModel):
    items: list[AgentTokenResponse]
    max_active: int


class AgentActivityItem(BaseModel):
    id: str
    created_at: datetime
    token_id: str
    token_name: str
    tool: str
    site_id: str | None
    summary: str


class AgentActivityListResponse(BaseModel):
    items: list[AgentActivityItem]
