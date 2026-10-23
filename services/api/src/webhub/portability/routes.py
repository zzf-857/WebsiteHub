from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from webhub.auth.dependencies import (
    CurrentIdentityDependency,
    DatabaseSessionDependency,
    SettingsDependency,
    require_trusted_origin,
)
from webhub.portability import importer
from webhub.portability.errors import PortabilityError
from webhub.portability.export import export_backup
from webhub.portability.schemas import ApplyRequest, ApplyResponse, PreviewResponse

router = APIRouter(prefix="/portability", tags=["portability"])
WriteOriginDependency = Annotated[None, Depends(require_trusted_origin)]


async def _call[T](action: Awaitable[T]) -> T:
    try:
        return await action
    except PortabilityError as error:
        raise HTTPException(
            error.status_code, detail={"code": error.code, "message": error.message}
        ) from error


@router.get("/export")
async def download_backup(
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    settings: SettingsDependency,
) -> StreamingResponse:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    await _call(
        importer.expire_previews(session, identity.user.id, settings.data_directory, now=now)
    )
    filename = f"webhub-backup-{identity.user.username}-{now:%Y%m%d}.json"
    fallback = filename if filename.isascii() else f"webhub-backup-account-{now:%Y%m%d}.json"
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(
        export_backup(session, identity.user, now=now),
        media_type="application/json",
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/imports", response_model=PreviewResponse, status_code=201)
async def upload_backup(
    request: Request,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    settings: SettingsDependency,
    _: WriteOriginDependency,
) -> PreviewResponse:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise HTTPException(
            422, detail={"code": "invalid_format", "message": "请选择 JSON 格式的 WebHub 备份"}
        )
    return await _call(
        importer.create_preview(
            session, identity.user.id, settings.data_directory, request.stream()
        )
    )


@router.get("/imports/{import_id}", response_model=PreviewResponse)
async def read_preview(
    import_id: str,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    settings: SettingsDependency,
) -> PreviewResponse:
    return await _call(
        importer.get_preview(session, identity.user.id, import_id, settings.data_directory)
    )


@router.post("/imports/{import_id}/apply", response_model=ApplyResponse)
async def confirm_import(
    import_id: str,
    payload: ApplyRequest,
    identity: CurrentIdentityDependency,
    session: DatabaseSessionDependency,
    settings: SettingsDependency,
    _: WriteOriginDependency,
) -> ApplyResponse:
    return await _call(
        importer.apply_preview(
            session, identity.user.id, import_id, payload.expected_sha256, settings.data_directory
        )
    )
