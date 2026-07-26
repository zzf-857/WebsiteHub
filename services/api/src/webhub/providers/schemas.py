from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)

from webhub.providers.registry import ProviderKind


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SecretWriteRequest(StrictRequest):
    action: Literal["write"]
    value: SecretStr = Field(min_length=1, max_length=8_192)


class SecretReplaceRequest(StrictRequest):
    action: Literal["replace"]
    value: SecretStr = Field(min_length=1, max_length=8_192)


class SecretClearRequest(StrictRequest):
    action: Literal["clear"]


SecretUpdateRequest = Annotated[
    SecretReplaceRequest | SecretClearRequest,
    Field(discriminator="action"),
]


class SecretTestRequest(StrictRequest):
    action: Literal["test"]
    value: SecretStr = Field(min_length=1, max_length=8_192)


class ProviderCreateRequest(StrictRequest):
    kind: ProviderKind
    provider: str = Field(min_length=1, max_length=48)
    display_name: str = Field(min_length=1, max_length=80)
    base_url: str | None = Field(default=None, max_length=2_048)
    model_name: str | None = Field(default=None, max_length=160)
    secret: SecretWriteRequest | None = None
    enabled: bool = False


class ProviderUpdateRequest(StrictRequest):
    expected_version: int = Field(
        ge=1,
        validation_alias=AliasChoices("expected_version", "expectedVersion"),
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    base_url: str | None = Field(default=None, max_length=2_048)
    model_name: str | None = Field(default=None, max_length=160)
    secret: SecretUpdateRequest | None = None
    enabled: bool | None = None


class ExpectedVersionRequest(StrictRequest):
    expected_version: int = Field(
        ge=1,
        validation_alias=AliasChoices("expected_version", "expectedVersion"),
    )


class ProviderConnectionTestRequest(StrictRequest):
    config_id: str | None = None
    expected_version: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices("expected_version", "expectedVersion"),
    )
    kind: ProviderKind | None = None
    provider: str | None = Field(default=None, min_length=1, max_length=48)
    base_url: str | None = Field(default=None, max_length=2_048)
    model_name: str | None = Field(default=None, max_length=160)
    secret: SecretTestRequest | None = None

    @model_validator(mode="after")
    def validate_source(self) -> ProviderConnectionTestRequest:
        if self.config_id is None:
            if self.expected_version is not None:
                raise ValueError("未指定 config_id 时不能提供 expected_version")
            if self.kind is None or self.provider is None:
                raise ValueError("测试未保存配置时必须提供 kind 和 provider")
        elif self.expected_version is None:
            raise ValueError("测试已保存配置时必须提供 expected_version")
        return self


class ProviderResponse(BaseModel):
    id: str
    kind: ProviderKind
    provider: str
    display_name: str
    base_url: str | None
    model_name: str | None
    enabled: bool
    has_secret: bool
    secret_mask: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class ProviderListResponse(BaseModel):
    items: list[ProviderResponse]


class ProviderDeleteResponse(BaseModel):
    message: str
    config_id: str


class ProviderRegistryItem(BaseModel):
    provider: str
    label: str
    kinds: list[ProviderKind]
    secret_required: bool
    base_url_required: bool
    allows_private_base_url: bool
    application_url: str | None
    connection_test_supported: bool


class ProviderRegistryResponse(BaseModel):
    items: list[ProviderRegistryItem]


class ProviderConnectionTestResponse(BaseModel):
    status: Literal["unsupported"]
    code: str
    message: str
    kind: ProviderKind
    provider: str
