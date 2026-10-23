"""Persistent previews and one atomic, account-scoped recovery transaction."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from webhub.bookmarks.normalization import normalize_bookmark_url
from webhub.db.locking import reserve_account_taxonomy
from webhub.db.models import Category, Feed, PortabilityImport, Site, Space, Tag, new_id
from webhub.feeds.backup import restore_feed
from webhub.feeds.service import FeedError, normalize_feed_url
from webhub.library.service import LibraryError
from webhub.library.service.backup import (
    SiteBackup,
    normalize_backup_name,
    restore_category,
    restore_site,
    restore_tag,
)
from webhub.portability.errors import PortabilityError
from webhub.portability.export import utc
from webhub.portability.schemas import (
    MAX_PAYLOAD_BYTES,
    ApplyResponse,
    BackupV1,
    PreviewCounts,
    PreviewResponse,
    ResultCounts,
    SampleResponse,
    WarningResponse,
)
from webhub.portability.validation import validate_backup
from webhub.spaces.backup import normalize_backup_name as normalize_space_name
from webhub.spaces.backup import restore_space
from webhub.spaces.service import SpaceError


def payload_path(directory: Path, user_id: str, import_id: str) -> Path:
    # IDs come from authenticated database rows / new_id, never the uploaded document.
    return directory / "portability-imports" / user_id / f"{import_id}.json"


async def expire_previews(
    session: AsyncSession, user_id: str, directory: Path, *, now: datetime | None = None
) -> None:
    current = now or datetime.now(UTC)
    ids = list(
        (
            await session.scalars(
                update(PortabilityImport)
                .where(
                    PortabilityImport.user_id == user_id,
                    PortabilityImport.status == "previewed",
                    PortabilityImport.expires_at <= current,
                )
                .values(status="expired")
                .returning(PortabilityImport.id)
            )
        ).all()
    )
    await session.commit()
    for import_id in ids:
        payload_path(directory, user_id, import_id).unlink(missing_ok=True)


async def _owned_import(session: AsyncSession, user_id: str, import_id: str) -> PortabilityImport:
    row = await session.scalar(
        select(PortabilityImport)
        .where(PortabilityImport.user_id == user_id, PortabilityImport.id == import_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise PortabilityError("not_found", 404, "导入预览不存在，请重新上传备份")
    return row


async def get_preview(
    session: AsyncSession,
    user_id: str,
    import_id: str,
    directory: Path,
    *,
    now: datetime | None = None,
) -> PreviewResponse:
    await expire_previews(session, user_id, directory, now=now)
    row = await _owned_import(session, user_id, import_id)
    if row.status == "expired":
        raise PortabilityError("preview_expired", 409, "预览已过期，请重新上传备份")
    result = PreviewResponse.model_validate_json(row.summary_json)
    return result.model_copy(update={"status": row.status})


async def build_preview(
    session: AsyncSession,
    user_id: str,
    backup: BackupV1,
    *,
    import_id: str,
    checksum: str,
    expires_at: datetime,
) -> PreviewResponse:
    counts = PreviewCounts()
    warnings: list[WarningResponse] = []
    samples: dict[str, list[SampleResponse]] = {}

    def sample(group: str, name: str, url: str | None = None) -> None:
        values = samples.setdefault(group, [])
        if len(values) < 20:
            values.append(SampleResponse(name=name, url=url))

    for section, model, maximum in (("categories", Category, 80), ("tags", Tag, 40)):
        names = set(
            (
                await session.scalars(select(model.normalized_name).where(model.user_id == user_id))
            ).all()
        )
        totals = getattr(counts, section)
        for row in getattr(backup, section):
            _, normalized = normalize_backup_name(row.name, maximum=maximum)
            action = "reuse" if normalized in names else "create"
            setattr(totals, action, getattr(totals, action) + 1)
            names.add(normalized)
            sample(f"{section}_{action}", row.name)
    urls = set(
        (await session.scalars(select(Site.identity_url).where(Site.user_id == user_id))).all()
    )
    for index, site in enumerate(backup.sites):
        identity = normalize_bookmark_url(site.url).normalized_url
        if identity is None:
            counts.sites.skip_invalid += 1
            warnings.append(
                WarningResponse(
                    code="invalid_url", message=f"第 {index + 1} 条收藏的网址无效，已跳过"
                )
            )
            sample("sites_skip_invalid", site.name, site.url)
            continue
        action = "merge" if identity in urls else "create"
        setattr(counts.sites, action, getattr(counts.sites, action) + 1)
        urls.add(identity)
        sample(f"sites_{action}", site.name, site.url)
    names = set(
        (await session.scalars(select(Space.normalized_name).where(Space.user_id == user_id))).all()
    )
    for space in backup.spaces:
        _, normalized = normalize_space_name(space.name)
        action = "merge" if normalized in names else "create"
        setattr(counts.spaces, action, getattr(counts.spaces, action) + 1)
        names.add(normalized)
        sample(f"spaces_{action}", space.name)
    urls = set((await session.scalars(select(Feed.url).where(Feed.user_id == user_id))).all())
    for feed in backup.feeds:
        normalized = normalize_feed_url(feed.url)
        action = "skip_existing" if normalized in urls else "create"
        setattr(counts.feeds, action, getattr(counts.feeds, action) + 1)
        urls.add(normalized)
        sample(f"feeds_{action}", feed.title or feed.url, feed.url)
    counts.conversations.skipped = len(backup.conversations)
    if backup.conversations:
        warnings.append(
            WarningResponse(
                code="conversations_skipped", message="v1 备份中的对话仅供保存，本次不会导入对话"
            )
        )
        for conversation in backup.conversations:
            sample("conversations_skipped", conversation.title)
    return PreviewResponse(
        id=import_id,
        status="previewed",
        expires_at=expires_at,
        payload_sha256=checksum,
        counts=counts,
        warnings=warnings,
        samples=samples,
    )


async def create_preview(
    session: AsyncSession,
    user_id: str,
    directory: Path,
    chunks: AsyncIterator[bytes],
    *,
    now: datetime | None = None,
) -> PreviewResponse:
    current = now or datetime.now(UTC)
    await expire_previews(session, user_id, directory, now=current)
    import_id = new_id()
    path = payload_path(directory, user_id, import_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    digest = hashlib.sha256()
    succeeded = False
    try:
        with path.open("xb") as destination:
            path.chmod(0o600)
            async for chunk in chunks:
                size += len(chunk)
                if size > MAX_PAYLOAD_BYTES:
                    raise PortabilityError(
                        "payload_too_large", 413, "备份超过 64 MiB，请选择较小的文件"
                    )
                destination.write(chunk)
                digest.update(chunk)
        backup = validate_backup(path.read_bytes())
        preview = await build_preview(
            session,
            user_id,
            backup,
            import_id=import_id,
            checksum=digest.hexdigest(),
            expires_at=current + timedelta(hours=24),
        )
        session.add(
            PortabilityImport(
                id=import_id,
                user_id=user_id,
                status="previewed",
                payload_sha256=preview.payload_sha256,
                payload_path=str(path),
                summary_json=preview.model_dump_json(),
                created_at=current,
                expires_at=preview.expires_at,
            )
        )
        await session.commit()
        succeeded = True
        return preview
    finally:
        if not succeeded:
            await session.rollback()
            path.unlink(missing_ok=True)


async def _restore(
    session: AsyncSession, user_id: str, backup: BackupV1, *, now: datetime
) -> tuple[ResultCounts, ResultCounts]:
    created, merged = ResultCounts(), ResultCounts()
    categories: dict[str, str] = {}
    tags: dict[str, str] = {}
    sites: dict[str, str] = {}
    for category in backup.categories:
        item_id, added = await restore_category(
            session, user_id, name=category.name, icon=category.icon
        )
        categories[category.key] = item_id
        (created if added else merged).categories += 1
    for tag in backup.tags:
        item_id, added = await restore_tag(session, user_id, name=tag.name)
        tags[tag.key] = item_id
        (created if added else merged).tags += 1
    for site in backup.sites:
        if normalize_bookmark_url(site.url).normalized_url is None:
            continue
        data = SiteBackup(**site.model_dump(exclude={"key", "category_key", "tag_keys"}))
        item_id, added = await restore_site(
            session,
            user_id,
            data,
            category_id=categories[site.category_key],
            tag_ids=[tags[key] for key in site.tag_keys],
        )
        sites[site.key] = item_id
        (created if added else merged).sites += 1
    for space in backup.spaces:
        _, added = await restore_space(
            session,
            user_id,
            name=space.name,
            site_ids=[sites[key] for key in space.site_keys if key in sites],
        )
        (created if added else merged).spaces += 1
    for feed in backup.feeds:
        _, added = await restore_feed(session, user_id, **feed.model_dump(), now=now)
        if added:
            created.feeds += 1
    return created, merged


async def apply_preview(
    session: AsyncSession,
    user_id: str,
    import_id: str,
    expected_sha256: str,
    directory: Path,
    *,
    now: datetime | None = None,
) -> ApplyResponse:
    current = now or datetime.now(UTC)
    # Finish independent expiry maintenance before opening the recovery transaction.
    await expire_previews(session, user_id, directory, now=current)
    await session.rollback()
    path = payload_path(directory, user_id, import_id)
    try:
        # Lock before reading: concurrent applies re-read the committed receipt after waiting.
        if not await reserve_account_taxonomy(session, user_id):
            raise PortabilityError("not_found", 404, "账号不存在")
        row = await _owned_import(session, user_id, import_id)
        effective_now = now or datetime.now(UTC)
        if row.status == "expired" or (
            row.status != "applied" and utc(row.expires_at) <= effective_now
        ):
            if row.status != "expired":
                row.status = "expired"
                await session.commit()
            path.unlink(missing_ok=True)
            raise PortabilityError("preview_expired", 409, "预览已过期，请重新上传备份")
        if not hmac.compare_digest(expected_sha256, row.payload_sha256):
            raise PortabilityError("checksum_mismatch", 409, "备份校验值不一致，请重新上传备份")
        if row.status == "applied":
            assert row.result_json is not None
            response = ApplyResponse.model_validate_json(row.result_json)
            await session.rollback()
            return response
        try:
            payload = path.read_bytes()
        except FileNotFoundError as error:
            raise PortabilityError("not_found", 404, "备份文件不存在，请重新上传") from error
        if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), row.payload_sha256):
            raise PortabilityError("checksum_mismatch", 409, "备份内容已变化，请重新上传备份")
        backup = validate_backup(payload)
        created, merged = await _restore(session, user_id, backup, now=current)
        response = ApplyResponse(id=row.id, applied_at=current, created=created, merged=merged)
        await session.execute(
            update(PortabilityImport)
            .where(PortabilityImport.user_id == user_id, PortabilityImport.id == import_id)
            .values(status="applied", applied_at=current, result_json=response.model_dump_json())
        )
        await session.commit()
    except (LibraryError, SpaceError, FeedError, SQLAlchemyError) as error:
        await session.rollback()
        raise PortabilityError("import_failed", 409, "导入未完成，数据未修改，请重试") from error
    finally:
        # Also rolls back cancellation or unexpected errors; never swallow the cause.
        if session.in_transaction():
            await session.rollback()
    path.unlink(missing_ok=True)
    return response
