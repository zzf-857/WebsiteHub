from __future__ import annotations

from dataclasses import dataclass
from typing import Never, cast

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.config import Settings
from webhub.db.models import ProviderConfig, new_id, utc_now
from webhub.providers.connectivity import (
    ProviderProbeError,
    probe_base_url,
    probe_models,
    probe_search,
)
from webhub.providers.registry import (
    PROVIDER_REGISTRY,
    ProviderDefinition,
    ProviderKind,
    provider_definition,
)
from webhub.providers.schemas import (
    ProviderConnectionTestRequest,
    ProviderConnectionTestResponse,
    ProviderCreateRequest,
    ProviderDeleteResponse,
    ProviderListResponse,
    ProviderRegistryItem,
    ProviderRegistryResponse,
    ProviderResponse,
    ProviderUpdateRequest,
    SecretClearRequest,
    SecretReplaceRequest,
)
from webhub.providers.security import (
    SECRET_MASK,
    ProviderSecretInvalidError,
    ProviderSecretUnavailableError,
    decrypt_secret,
    encrypt_secret,
)
from webhub.providers.targets import (
    ProviderTargetError,
    normalize_base_url,
)


class ProviderError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ProviderNotFoundError(ProviderError):
    def __init__(self) -> None:
        super().__init__(404, "not_found", "Provider 配置不存在")


class ProviderValidationError(ProviderError):
    def __init__(self, message: str, *, code: str = "validation_error") -> None:
        super().__init__(422, code, message)


class ProviderConflictError(ProviderError):
    def __init__(self, message: str, *, code: str = "conflict") -> None:
        super().__init__(409, code, message)


@dataclass(frozen=True, slots=True)
class _PreparedConfig:
    definition: ProviderDefinition
    base_url: str | None
    model_name: str | None


def _raise_secret_error(error: Exception) -> Never:
    if isinstance(error, ProviderSecretUnavailableError):
        # WebHub is self-hosted and usually single-user: "contact your service
        # administrator" tells the person reading it to go ask themselves.  Say
        # what is actually wrong and what fixes it.
        raise ProviderError(
            503,
            "provider_key_unavailable",
            "服务端缺少用于加密 API Key 的主密钥，无法保存。"
            "开发环境重启一次服务即可自动生成；"
            "生产环境需要设置 WEBHUB_PROVIDER_MASTER_KEY（base64 编码的 32 字节）。",
        ) from error
    raise ProviderError(
        503,
        "provider_secret_invalid",
        "Provider 凭据无法安全读取，请重新填写 API Key",
    ) from error


def _display_name(value: str) -> str:
    normalized = " ".join(value.split())
    if not 1 <= len(normalized) <= 80:
        raise ProviderValidationError("配置名称长度必须为 1 到 80 个字符")
    return normalized


def _model_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 160:
        raise ProviderValidationError("模型名称不能超过 160 个字符")
    return normalized


def _prepare(
    *,
    kind: ProviderKind,
    provider: str,
    base_url: str | None,
    model_name: str | None,
) -> _PreparedConfig:
    try:
        definition = provider_definition(kind, provider)
    except ValueError as error:
        raise ProviderValidationError(
            str(error),
            code="unsupported_provider",
        ) from error
    try:
        # Fixed keyless adapters always use the audited official endpoint.  We
        # deliberately normalize them to ``None`` in storage so stale clients
        # cannot smuggle a hidden URL into a form that has no address field.
        normalized_base_url = (
            None
            if definition.fixed_base_url
            else normalize_base_url(
                base_url,
                allow_private=definition.allows_private_base_url,
            )
        )
    except ProviderTargetError as error:
        raise ProviderValidationError(error.message, code=error.code) from error
    normalized_model_name = _model_name(model_name)
    if kind == "search" and normalized_model_name is not None:
        raise ProviderValidationError("搜索 Provider 不能设置模型名称")
    return _PreparedConfig(definition, normalized_base_url, normalized_model_name)


def _validate_complete(
    *,
    kind: ProviderKind,
    prepared: _PreparedConfig,
    has_secret: bool,
    require_model_name: bool = True,
) -> None:
    missing: list[str] = []
    if prepared.definition.secret_required and not has_secret:
        missing.append("API Key")
    if prepared.definition.base_url_required and prepared.base_url is None:
        missing.append("Base URL")
    # A connection test lists the vendor's models; demanding a model name up
    # front would make the list unreachable for the very user who needs it.
    if require_model_name and kind in {"model", "embedding"} and prepared.model_name is None:
        missing.append("模型名称")
    if missing:
        raise ProviderValidationError(
            f"配置尚不完整，缺少：{'、'.join(missing)}",
            code="provider_config_incomplete",
        )


def _has_secret(config: ProviderConfig) -> bool:
    if (config.secret_ciphertext is None) != (config.secret_nonce is None):
        raise ProviderError(
            503,
            "provider_secret_invalid",
            "Provider 凭据状态损坏，请重新填写 API Key",
        )
    return config.secret_ciphertext is not None


def _response(config: ProviderConfig) -> ProviderResponse:
    has_secret = _has_secret(config)
    return ProviderResponse(
        id=config.id,
        kind=cast(ProviderKind, config.kind),
        provider=config.provider,
        display_name=config.display_name,
        base_url=config.base_url,
        model_name=config.model_name,
        enabled=config.enabled,
        has_secret=has_secret,
        secret_mask=SECRET_MASK if has_secret else None,
        version=config.version,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


async def _owned_config(
    session: AsyncSession,
    user_id: str,
    config_id: str,
) -> ProviderConfig:
    config = await session.scalar(
        select(ProviderConfig).where(
            ProviderConfig.user_id == user_id,
            ProviderConfig.id == config_id,
        )
    )
    if config is None:
        raise ProviderNotFoundError
    return config


def registry() -> ProviderRegistryResponse:
    kind_order = {"model": 0, "search": 1, "embedding": 2}
    return ProviderRegistryResponse(
        items=[
            ProviderRegistryItem(
                provider=item.provider,
                label=item.label,
                kinds=sorted(item.kinds, key=kind_order.__getitem__),
                secret_required=item.secret_required,
                base_url_required=item.base_url_required,
                allows_private_base_url=item.allows_private_base_url,
                application_url=item.application_url,
                connection_test_supported=item.connection_test_supported,
                search_bulk_supported=item.search_bulk_supported,
                usage_notice=item.usage_notice,
                fixed_base_url=item.fixed_base_url,
                default_base_url=item.default_base_url,
            )
            for item in PROVIDER_REGISTRY
        ]
    )


async def list_configs(
    session: AsyncSession,
    user_id: str,
    *,
    kind: ProviderKind | None,
) -> ProviderListResponse:
    query = select(ProviderConfig).where(ProviderConfig.user_id == user_id)
    if kind is not None:
        query = query.where(ProviderConfig.kind == kind)
    configs = list(
        (
            await session.scalars(
                query.order_by(
                    ProviderConfig.kind,
                    ProviderConfig.enabled.desc(),
                    ProviderConfig.updated_at.desc(),
                    ProviderConfig.id,
                )
            )
        ).all()
    )
    return ProviderListResponse(items=[_response(config) for config in configs])


async def get_config(
    session: AsyncSession,
    user_id: str,
    config_id: str,
) -> ProviderResponse:
    return _response(await _owned_config(session, user_id, config_id))


async def create_config(
    session: AsyncSession,
    user_id: str,
    settings: Settings,
    payload: ProviderCreateRequest,
) -> ProviderResponse:
    prepared = _prepare(
        kind=payload.kind,
        provider=payload.provider,
        base_url=payload.base_url,
        model_name=payload.model_name,
    )
    config_id = new_id()
    ciphertext: bytes | None = None
    nonce: bytes | None = None
    key_version = settings.provider_master_key_version
    if payload.secret is not None:
        try:
            ciphertext, nonce, key_version = encrypt_secret(
                settings,
                payload.secret.value.get_secret_value(),
                user_id=user_id,
                config_id=config_id,
                kind=payload.kind,
                provider=payload.provider,
            )
        except ProviderSecretUnavailableError as error:
            _raise_secret_error(error)
    has_secret = ciphertext is not None
    if payload.enabled:
        _validate_complete(kind=payload.kind, prepared=prepared, has_secret=has_secret)

    now = utc_now()
    config = ProviderConfig(
        id=config_id,
        user_id=user_id,
        kind=payload.kind,
        provider=payload.provider,
        display_name=_display_name(payload.display_name),
        base_url=prepared.base_url,
        model_name=prepared.model_name,
        secret_ciphertext=ciphertext,
        secret_nonce=nonce,
        key_version=key_version,
        enabled=payload.enabled,
        version=1,
        created_at=now,
        updated_at=now,
    )
    try:
        if payload.enabled:
            await session.execute(
                update(ProviderConfig)
                .where(
                    ProviderConfig.user_id == user_id,
                    ProviderConfig.kind == payload.kind,
                    ProviderConfig.enabled.is_(True),
                )
                .values(
                    enabled=False,
                    version=ProviderConfig.version + 1,
                    updated_at=now,
                )
            )
        session.add(config)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise ProviderConflictError(
            "同类型中已存在同名配置",
            code="duplicate_provider_name",
        ) from error
    return _response(config)


async def update_config(
    session: AsyncSession,
    user_id: str,
    settings: Settings,
    config_id: str,
    payload: ProviderUpdateRequest,
) -> ProviderResponse:
    config = await _owned_config(session, user_id, config_id)
    if config.version != payload.expected_version:
        raise ProviderConflictError(
            "Provider 配置已被修改，请刷新后重试",
            code="version_conflict",
        )
    fields = payload.model_fields_set - {"expected_version"}
    if not fields:
        raise ProviderValidationError("Provider 更新至少需要一个字段")

    kind = cast(ProviderKind, config.kind)
    display_name = config.display_name
    if "display_name" in fields:
        if payload.display_name is None:
            raise ProviderValidationError("配置名称不能为空")
        display_name = _display_name(payload.display_name)

    base_url = payload.base_url if "base_url" in fields else config.base_url
    model_name = payload.model_name if "model_name" in fields else config.model_name
    prepared = _prepare(
        kind=kind,
        provider=config.provider,
        base_url=base_url,
        model_name=model_name,
    )

    ciphertext = config.secret_ciphertext
    nonce = config.secret_nonce
    key_version = config.key_version
    has_secret = _has_secret(config)
    secret_cleared = False
    if "secret" in fields:
        if payload.secret is None:
            raise ProviderValidationError(
                "保留原 API Key 时请省略 secret 字段",
                code="invalid_secret_action",
            )
        if isinstance(payload.secret, SecretReplaceRequest):
            try:
                ciphertext, nonce, key_version = encrypt_secret(
                    settings,
                    payload.secret.value.get_secret_value(),
                    user_id=user_id,
                    config_id=config.id,
                    kind=config.kind,
                    provider=config.provider,
                )
            except ProviderSecretUnavailableError as error:
                _raise_secret_error(error)
            has_secret = True
        elif isinstance(payload.secret, SecretClearRequest):
            ciphertext = None
            nonce = None
            key_version = settings.provider_master_key_version
            has_secret = False
            secret_cleared = True

    if "enabled" in fields and payload.enabled is None:
        raise ProviderValidationError("启用状态不能为空")
    enabled = payload.enabled if "enabled" in fields else config.enabled
    if secret_cleared and prepared.definition.secret_required:
        if payload.enabled is True:
            raise ProviderValidationError(
                "清除 API Key 时不能同时启用该配置",
                code="provider_config_incomplete",
            )
        enabled = False
    if enabled:
        _validate_complete(kind=kind, prepared=prepared, has_secret=has_secret)

    now = utc_now()
    try:
        if enabled:
            await session.execute(
                update(ProviderConfig)
                .where(
                    ProviderConfig.user_id == user_id,
                    ProviderConfig.kind == config.kind,
                    ProviderConfig.id != config.id,
                    ProviderConfig.enabled.is_(True),
                )
                .values(
                    enabled=False,
                    version=ProviderConfig.version + 1,
                    updated_at=now,
                )
            )
        claimed = await session.execute(
            update(ProviderConfig)
            .where(
                ProviderConfig.user_id == user_id,
                ProviderConfig.id == config.id,
                ProviderConfig.version == payload.expected_version,
            )
            .values(
                display_name=display_name,
                base_url=prepared.base_url,
                model_name=prepared.model_name,
                secret_ciphertext=ciphertext,
                secret_nonce=nonce,
                key_version=key_version,
                enabled=enabled,
                version=ProviderConfig.version + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise ProviderConflictError(
                "Provider 配置已被修改，请刷新后重试",
                code="version_conflict",
            )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise ProviderConflictError(
            "同类型中已存在同名配置",
            code="duplicate_provider_name",
        ) from error
    await session.refresh(config)
    return _response(config)


async def enable_config(
    session: AsyncSession,
    user_id: str,
    settings: Settings,
    config_id: str,
    *,
    expected_version: int,
) -> ProviderResponse:
    return await update_config(
        session,
        user_id,
        settings,
        config_id,
        ProviderUpdateRequest(expected_version=expected_version, enabled=True),
    )


async def delete_config(
    session: AsyncSession,
    user_id: str,
    config_id: str,
    *,
    expected_version: int,
) -> ProviderDeleteResponse:
    await _owned_config(session, user_id, config_id)
    deleted = await session.execute(
        delete(ProviderConfig)
        .where(
            ProviderConfig.user_id == user_id,
            ProviderConfig.id == config_id,
            ProviderConfig.version == expected_version,
        )
        .execution_options(synchronize_session=False)
    )
    if deleted.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise ProviderConflictError(
            "Provider 配置已被修改，请刷新后重试",
            code="version_conflict",
        )
    await session.commit()
    return ProviderDeleteResponse(message="Provider 配置已删除", config_id=config_id)


async def test_connection(
    session: AsyncSession,
    user_id: str,
    settings: Settings,
    payload: ProviderConnectionTestRequest,
) -> ProviderConnectionTestResponse:
    stored: ProviderConfig | None = None
    if payload.config_id is not None:
        stored = await _owned_config(session, user_id, payload.config_id)
        if stored.version != payload.expected_version:
            raise ProviderConflictError(
                "Provider 配置已被修改，请刷新后重试",
                code="version_conflict",
            )

    if settings.provider_master_key is None:
        _raise_secret_error(ProviderSecretUnavailableError("missing master key"))

    kind = payload.kind or cast(ProviderKind, stored.kind if stored else "")
    provider = payload.provider or (stored.provider if stored else "")
    base_url = (
        payload.base_url
        if "base_url" in payload.model_fields_set
        else (stored.base_url if stored else None)
    )
    model_name = (
        payload.model_name
        if "model_name" in payload.model_fields_set
        else (stored.model_name if stored else None)
    )
    prepared = _prepare(
        kind=kind,
        provider=provider,
        base_url=base_url,
        model_name=model_name,
    )

    secret_value: str | None = None
    try:
        if payload.secret is not None:
            secret_value = payload.secret.value.get_secret_value()
        elif (
            stored is not None
            and stored.kind == kind
            and stored.provider == provider
            and _has_secret(stored)
        ):
            try:
                secret_value = decrypt_secret(
                    settings,
                    stored.secret_ciphertext or b"",
                    stored.secret_nonce or b"",
                    stored.key_version,
                    user_id=user_id,
                    config_id=stored.id,
                    kind=stored.kind,
                    provider=stored.provider,
                )
            except (ProviderSecretUnavailableError, ProviderSecretInvalidError) as error:
                _raise_secret_error(error)
        _validate_complete(
            kind=kind,
            prepared=prepared,
            has_secret=secret_value is not None,
            # Neither probe needs a model name: model probes list the catalogue,
            # while search probes use the Provider's search endpoint directly.
            require_model_name=False,
        )

        # Only adapters with an explicit probe may expose the button.  Model
        # probes are read-only; search probes make one minimal, potentially
        # billable query after the user clicks the clearly labelled action.
        if not prepared.definition.connection_test_supported:
            return ProviderConnectionTestResponse(
                status="unsupported",
                code="connection_test_unsupported",
                message="该服务商尚未实现连接测试，未发送任何外部请求",
                kind=kind,
                provider=provider,
            )

        try:
            probe_base = probe_base_url(prepared.definition, prepared.base_url)
        except ProviderProbeError as error:
            raise ProviderValidationError(error.message, code=error.code) from error

        try:
            if kind == "search":
                search_result = await probe_search(
                    prepared.definition,
                    base_url=probe_base,
                    api_key=secret_value,
                    timeout_seconds=settings.provider_test_timeout_seconds,
                )
                model_result = None
            else:
                model_result = await probe_models(
                    prepared.definition,
                    base_url=probe_base,
                    api_key=secret_value,
                    timeout_seconds=settings.provider_test_timeout_seconds,
                )
                search_result = None
        except ProviderProbeError as error:
            # A failed probe is a normal outcome, not a server fault: answer 200
            # with status="failed" so the client can render it inline. Nothing
            # was written, so the stored config and the enabled config are
            # untouched by construction.
            return ProviderConnectionTestResponse(
                status="failed",
                code=error.code,
                message=error.message,
                kind=kind,
                provider=provider,
            )

        if search_result is not None:
            return ProviderConnectionTestResponse(
                status="ok",
                code="connection_test_ok",
                message=(
                    f"连接成功，测试搜索返回 {search_result.result_count} 条结果。"
                    "本次请求可能计入服务商额度"
                ),
                kind=kind,
                provider=provider,
            )

        assert model_result is not None
        return ProviderConnectionTestResponse(
            status="ok",
            code="connection_test_ok",
            message=(
                f"连接成功，读取到 {len(model_result.models)} 个模型"
                if model_result.models
                else "连接成功，但该服务商没有返回任何模型"
            ),
            kind=kind,
            provider=provider,
            models=model_result.models,
        )
    finally:
        secret_value = None
