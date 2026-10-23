from __future__ import annotations

import json
import math
from typing import NoReturn

from pydantic import ValidationError

from webhub.feeds.service import FeedError, normalize_feed_url
from webhub.library.service import LibraryError
from webhub.library.service.backup import normalize_backup_name
from webhub.portability.errors import PortabilityError
from webhub.portability.schemas import MAX_PAYLOAD_BYTES, BackupV1
from webhub.spaces.backup import normalize_backup_name as normalize_space_name
from webhub.spaces.service import SpaceError


def invalid(path: str) -> NoReturn:
    raise PortabilityError("invalid_format", 422, f"备份格式无效：{path}，请重新选择备份文件")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            invalid("重复字段")
        result[key] = value
    return result


def _constant(_: str) -> NoReturn:
    invalid("非有限数字")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        invalid("非有限数字")
    return number


def validate_backup(payload: bytes) -> BackupV1:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise PortabilityError("payload_too_large", 413, "备份超过 64 MiB，请选择较小的文件")
    try:
        value = json.loads(
            payload, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_finite_float
        )
    except (ValueError, RecursionError, UnicodeError):
        invalid("JSON")
    if not isinstance(value, dict):
        invalid("$")
    if "version" in value and value["version"] != 1:
        raise PortabilityError("unsupported_version", 422, "暂不支持这个备份版本，请使用 v1 备份")
    if type(value.get("version")) is not int:
        invalid("version")
    try:
        backup = BackupV1.model_validate(value)
    except ValidationError as error:
        first = error.errors(include_input=False, include_url=False)[0]
        path = ".".join(str(item) for item in first["loc"])
        if first["type"] in {"string_too_long", "too_long", "less_than_equal"}:
            raise PortabilityError(
                "limits_exceeded", 422, f"备份超过允许上限：{path}，请检查文件"
            ) from error
        invalid(path)
    for section, maximum in (("categories", 80), ("tags", 40), ("sites", 160), ("spaces", 120)):
        rows = getattr(backup, section)
        keys: set[str] = set()
        for index, row in enumerate(rows):
            if row.key in keys:
                invalid(f"{section}.{index}.key")
            keys.add(row.key)
            try:
                if section == "spaces":
                    normalize_space_name(row.name)
                else:
                    normalize_backup_name(row.name, maximum=maximum)
            except (LibraryError, SpaceError):
                invalid(f"{section}.{index}.name")
    category_keys = {row.key for row in backup.categories}
    tag_keys = {row.key for row in backup.tags}
    site_keys = {row.key for row in backup.sites}
    for index, row in enumerate(backup.sites):
        if row.category_key not in category_keys:
            invalid(f"sites.{index}.category_key")
        for position, key in enumerate(row.tag_keys):
            if key not in tag_keys:
                invalid(f"sites.{index}.tag_keys.{position}")
    for index, space in enumerate(backup.spaces):
        for position, key in enumerate(space.site_keys):
            if key not in site_keys:
                invalid(f"spaces.{index}.site_keys.{position}")
    for index, feed in enumerate(backup.feeds):
        try:
            normalize_feed_url(feed.url)
        except FeedError:
            invalid(f"feeds.{index}.url")
    return backup
