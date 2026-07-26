from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from webhub.bookmarks import routes as bookmark_routes
from webhub.bookmarks import uploads
from webhub.bookmarks.models import ParserLimits
from webhub.config import Settings
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import BookmarkImportJob, utc_now
from webhub.main import create_app

COOKIE_NAME = "webhub_session"
TRUSTED_ORIGIN = "http://testserver"
UPLOAD_PATH = "/api/bookmark-imports"
MOCK_EXPORT = Path(__file__).resolve().parents[4] / "MockData" / "bookmarks_2026_7_26.html"
POST_FIELDS = {
    "job_id",
    "state",
    "job_version",
    "replayed",
    "same_source_warning",
}
STATUS_FIELDS = {
    "job_id",
    "state",
    "job_version",
    "preview_version",
    "progress",
    "failure_code",
    "created_at",
    "updated_at",
    "completed_at",
}
PRIVATE_KEY_PARTS = ("path", "hash", "sha256", "snapshot", "storage")


@dataclass(frozen=True, slots=True)
class UploadApiEnvironment:
    client: TestClient
    data_directory: Path
    alice_id: str
    alice_token: str
    bob_id: str
    bob_token: str


def _export(label: str) -> bytes:
    return (
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>\n"
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">\n'
        f'<DL><p><DT><A HREF="https://example.com/{label}">{label}</A></DL><p>\n'
    ).encode()


def _register(client: TestClient, username: str) -> tuple[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "a sufficiently secure password"},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert response.status_code == 201, response.text
    token = response.cookies.get(COOKIE_NAME)
    assert token
    return str(response.json()["user"]["id"]), token


def _use_token(client: TestClient, token: str | None) -> None:
    client.cookies.clear()
    if token is not None:
        client.cookies.set(COOKIE_NAME, token)


def _upload_headers(
    key: str,
    *,
    content_type: str = "text/html",
    origin: str | None = TRUSTED_ORIGIN,
) -> dict[str, str]:
    headers = {"Idempotency-Key": key, "Content-Type": content_type}
    if origin is not None:
        headers["Origin"] = origin
    return headers


def _post_upload(
    client: TestClient,
    payload: bytes | Iterable[bytes],
    *,
    key: str,
    content_type: str = "text/html",
    origin: str | None = TRUSTED_ORIGIN,
    extra_headers: dict[str, str] | None = None,
):
    headers = _upload_headers(key, content_type=content_type, origin=origin)
    headers.update(extra_headers or {})
    return client.post(UPLOAD_PATH, content=payload, headers=headers)


def _all_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _all_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _all_keys(nested)


def _assert_public_payload(payload: object, data_directory: Path) -> None:
    for key in _all_keys(payload):
        normalized = key.casefold()
        assert not any(part in normalized for part in PRIVATE_KEY_PARTS), key
    serialized = json.dumps(payload, ensure_ascii=False).casefold()
    assert "bookmark-imports/" not in serialized
    assert str(data_directory).casefold() not in serialized


def _assert_no_incoming_files(data_directory: Path) -> None:
    incoming = data_directory / "bookmark-imports" / "incoming"
    if incoming.exists():
        assert [path for path in incoming.rglob("*") if path.is_file()] == []


def _assert_created_response(response, environment: UploadApiEnvironment) -> dict[str, object]:
    assert response.status_code == 201, response.text
    payload = response.json()
    assert set(payload) == POST_FIELDS
    assert payload["state"] == "queued_parse"
    assert payload["job_version"] == 2
    assert payload["replayed"] is False
    assert payload["same_source_warning"] is False
    assert response.headers["location"] == f"/api/bookmark-imports/{payload['job_id']}"
    _assert_public_payload(payload, environment.data_directory)
    return payload


@pytest.fixture
def upload_api_environment(tmp_path: Path) -> Iterator[UploadApiEnvironment]:
    database_path = tmp_path / "main.sqlite3"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        data_directory=tmp_path,
    )
    upgrade_database(settings.database_url)
    with TestClient(create_app(settings=settings)) as client:
        alice_id, alice_token = _register(client, "upload-alice")
        bob_id, bob_token = _register(client, "upload-bob")
        yield UploadApiEnvironment(
            client=client,
            data_directory=tmp_path,
            alice_id=alice_id,
            alice_token=alice_token,
            bob_id=bob_id,
            bob_token=bob_token,
        )


def test_upload_openapi_declares_raw_binary_contract_and_all_public_outcomes(
    upload_api_environment: UploadApiEnvironment,
) -> None:
    response = upload_api_environment.client.get("/api/openapi.json")
    assert response.status_code == 200, response.text
    operation = response.json()["paths"][UPLOAD_PATH]["post"]

    request_body = operation["requestBody"]
    assert request_body["required"] is True
    content = request_body["content"]
    assert set(content) == {"text/html", "application/octet-stream"}
    for media_type in content:
        body_schema = content[media_type]["schema"]
        assert body_schema["type"] == "string"
        assert body_schema["format"] == "binary"
        assert "properties" not in body_schema

    idempotency_headers = [
        parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "header"
        and parameter["name"].casefold() == "idempotency-key"
    ]
    assert len(idempotency_headers) == 1
    idempotency_header = idempotency_headers[0]
    assert idempotency_header["required"] is True
    assert idempotency_header["schema"]["minLength"] == 16
    assert idempotency_header["schema"]["maxLength"] == 512

    required_responses = {
        "200",
        "201",
        "400",
        "401",
        "403",
        "409",
        "413",
        "415",
        "429",
        "422",
        "507",
    }
    assert required_responses <= set(operation["responses"])


def test_upload_requires_login_and_trusted_origin(
    upload_api_environment: UploadApiEnvironment,
) -> None:
    environment = upload_api_environment
    client = environment.client
    payload = _export("auth-origin")

    _use_token(client, None)
    unauthenticated = _post_upload(
        client,
        payload,
        key="upload-auth-origin-0001",
    )
    assert unauthenticated.status_code == 401

    _use_token(client, environment.alice_token)
    missing_origin = _post_upload(
        client,
        payload,
        key="upload-auth-origin-0002",
        origin=None,
    )
    assert missing_origin.status_code == 403
    forged_origin = _post_upload(
        client,
        payload,
        key="upload-auth-origin-0003",
        origin="http://attacker.invalid",
    )
    assert forged_origin.status_code == 403
    _assert_no_incoming_files(environment.data_directory)


def test_upload_accepts_only_raw_html_or_octet_stream(
    upload_api_environment: UploadApiEnvironment,
) -> None:
    environment = upload_api_environment
    client = environment.client
    _use_token(client, environment.alice_token)

    html = _post_upload(
        client,
        _export("html-content-type"),
        key="upload-content-type-html-0001",
        content_type="text/html; charset=UTF-8",
    )
    _assert_created_response(html, environment)

    octets = _post_upload(
        client,
        _export("octet-content-type"),
        key="upload-content-type-octet-0001",
        content_type="application/octet-stream",
    )
    _assert_created_response(octets, environment)

    rejected_bodies = (
        ("application/json", b'{"bookmarks": []}', "upload-content-type-json-0001"),
        (
            "application/x-netscape-bookmarks",
            _export("non-contract-media-type"),
            "upload-content-type-netscape-0001",
        ),
        (
            "multipart/form-data; boundary=bookmark-test",
            b"--bookmark-test\r\nContent-Disposition: form-data; name=file\r\n\r\nx",
            "upload-content-type-multipart-0001",
        ),
    )
    for content_type, body, key in rejected_bodies:
        response = _post_upload(
            client,
            body,
            key=key,
            content_type=content_type,
        )
        assert response.status_code == 415, response.text


def test_upload_rejects_invalid_key_empty_file_format_and_encoding(
    upload_api_environment: UploadApiEnvironment,
) -> None:
    environment = upload_api_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    valid_export = _export("validation")

    missing_key = client.post(
        UPLOAD_PATH,
        content=valid_export,
        headers={"Origin": TRUSTED_ORIGIN, "Content-Type": "text/html"},
    )
    assert missing_key.status_code == 422
    for key in ("short", "x" * 513):
        response = _post_upload(client, valid_export, key=key)
        assert response.status_code == 422, response.text

    invalid_documents = (
        (b"", "upload-empty-file-request-0001"),
        (b"<html><a href='https://example.com'>Not an export</a></html>", "upload-format-0001"),
        (
            b"<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<meta charset=UTF-8>\n\xff",
            "upload-encoding-0001",
        ),
    )
    for body, key in invalid_documents:
        response = _post_upload(client, body, key=key)
        assert response.status_code == 422, response.text
        _assert_public_payload(response.json(), environment.data_directory)
    _assert_no_incoming_files(environment.data_directory)


def test_upload_enforces_declared_and_streamed_size_limits(
    upload_api_environment: UploadApiEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = upload_api_environment
    client = environment.client
    _use_token(client, environment.alice_token)

    def body_must_not_be_read() -> Iterator[bytes]:
        raise AssertionError("declared oversize request body was consumed")
        yield b""

    declared = _post_upload(
        client,
        body_must_not_be_read(),
        key="upload-declared-overflow-0001",
        extra_headers={"Content-Length": str(ParserLimits().max_file_bytes + 1)},
    )
    assert declared.status_code == 413, declared.text

    tiny_maximum = 64
    tiny_limits = ParserLimits(max_file_bytes=tiny_maximum)
    monkeypatch.setattr(uploads, "ParserLimits", lambda: tiny_limits)
    if hasattr(bookmark_routes, "ParserLimits"):
        monkeypatch.setattr(bookmark_routes, "ParserLimits", lambda: tiny_limits)

    payload = _export("streamed-overflow")

    def streamed_body() -> Iterator[bytes]:
        for offset in range(0, len(payload), 17):
            yield payload[offset : offset + 17]

    streamed = _post_upload(
        client,
        streamed_body(),
        key="upload-streamed-overflow-0001",
    )
    assert streamed.status_code == 413, streamed.text
    _assert_public_payload(streamed.json(), environment.data_directory)
    _assert_no_incoming_files(environment.data_directory)


def test_account_quota_rejects_a_second_upload_before_reading_its_body(
    upload_api_environment: UploadApiEnvironment,
) -> None:
    environment = upload_api_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    payload = _export("account-quota-first")
    client.app.state.bookmark_upload_admission.account_quota_bytes = len(payload)

    first = _post_upload(
        client,
        payload,
        key="upload-account-quota-first-0001",
    )
    _assert_created_response(first, environment)

    body_read = False

    def body_must_not_be_read() -> Iterator[bytes]:
        nonlocal body_read
        body_read = True
        raise AssertionError("account quota rejection consumed the request body")
        yield b""

    second = _post_upload(
        client,
        body_must_not_be_read(),
        key="upload-account-quota-second-0001",
        extra_headers={"Content-Length": "1"},
    )
    assert second.status_code == 413, second.text
    assert body_read is False
    _assert_public_payload(second.json(), environment.data_directory)
    _assert_no_incoming_files(environment.data_directory)


@pytest.mark.parametrize(
    "error_number",
    sorted({errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}),
)
def test_upload_maps_storage_exhaustion_to_507(
    upload_api_environment: UploadApiEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    environment = upload_api_environment
    client = environment.client
    _use_token(client, environment.alice_token)

    async def fail_stage(*_args: object, **_kwargs: object) -> None:
        raise OSError(error_number, "simulated bookmark storage exhaustion")

    monkeypatch.setattr(bookmark_routes, "stage_bookmark_upload", fail_stage)
    response = _post_upload(
        client,
        _export("storage-exhaustion"),
        key=f"upload-storage-exhaustion-{error_number:04d}",
    )

    assert response.status_code == 507, response.text
    assert response.json() == {"detail": "服务器暂时没有足够的书签导入存储空间"}
    assert client.app.state.bookmark_upload_admission.active_upload_count == 0
    _assert_public_payload(response.json(), environment.data_directory)
    _assert_no_incoming_files(environment.data_directory)


def test_upload_maps_sqlite_full_to_507_and_discards_staging(
    upload_api_environment: UploadApiEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = upload_api_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    database_error = sqlite3.OperationalError("database or disk is full")
    database_error.sqlite_errorcode = sqlite3.SQLITE_FULL
    database_error.sqlite_errorname = "SQLITE_FULL"

    async def fail_intake(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("INSERT INTO bookmark_import_jobs", {}, database_error)

    monkeypatch.setattr(bookmark_routes.intake, "intake_bookmark_upload", fail_intake)
    response = _post_upload(
        client,
        _export("sqlite-full"),
        key="upload-sqlite-full-request-0001",
    )

    assert response.status_code == 507, response.text
    assert response.json() == {"detail": "服务器暂时没有足够的书签导入存储空间"}
    assert client.app.state.bookmark_upload_admission.active_upload_count == 0
    _assert_public_payload(response.json(), environment.data_directory)
    _assert_no_incoming_files(environment.data_directory)


def test_upload_idempotency_conflict_and_same_source_warning(
    upload_api_environment: UploadApiEnvironment,
) -> None:
    environment = upload_api_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    payload = _export("idempotent-source")
    key = "upload-idempotent-request-0001"

    created = _assert_created_response(_post_upload(client, payload, key=key), environment)
    replay = _post_upload(client, payload, key=key)
    assert replay.status_code == 200, replay.text
    replay_payload = replay.json()
    assert set(replay_payload) == POST_FIELDS
    assert replay_payload["job_id"] == created["job_id"]
    assert replay_payload["state"] == "queued_parse"
    assert replay_payload["job_version"] == 2
    assert replay_payload["replayed"] is True
    assert replay_payload["same_source_warning"] is False
    assert replay.headers["location"] == f"/api/bookmark-imports/{created['job_id']}"
    _assert_public_payload(replay_payload, environment.data_directory)

    conflict = _post_upload(client, _export("different-source"), key=key)
    assert conflict.status_code == 409, conflict.text

    same_source = _post_upload(
        client,
        payload,
        key="upload-same-source-new-key-0001",
    )
    assert same_source.status_code == 201, same_source.text
    same_source_payload = same_source.json()
    assert set(same_source_payload) == POST_FIELDS
    assert same_source_payload["job_id"] != created["job_id"]
    assert same_source_payload["state"] == "queued_parse"
    assert same_source_payload["job_version"] == 2
    assert same_source_payload["replayed"] is False
    assert same_source_payload["same_source_warning"] is True
    assert same_source.headers["location"] == (
        f"/api/bookmark-imports/{same_source_payload['job_id']}"
    )
    _assert_public_payload(same_source_payload, environment.data_directory)
    _assert_no_incoming_files(environment.data_directory)


def test_upload_and_status_are_account_scoped(
    upload_api_environment: UploadApiEnvironment,
) -> None:
    environment = upload_api_environment
    client = environment.client
    shared_key = "upload-cross-account-request-0001"

    _use_token(client, environment.alice_token)
    alice = _assert_created_response(
        _post_upload(client, _export("alice-source"), key=shared_key),
        environment,
    )

    _use_token(client, environment.bob_token)
    assert client.get(f"{UPLOAD_PATH}/{alice['job_id']}").status_code == 404
    bob = _assert_created_response(
        _post_upload(client, _export("bob-different-source"), key=shared_key),
        environment,
    )
    assert bob["job_id"] != alice["job_id"]

    _use_token(client, environment.alice_token)
    assert client.get(f"{UPLOAD_PATH}/{bob['job_id']}").status_code == 404
    assert client.get(f"{UPLOAD_PATH}/00000000-0000-0000-0000-000000000000").status_code == 404


def test_job_status_is_no_store_and_exposes_only_public_state(
    upload_api_environment: UploadApiEnvironment,
) -> None:
    environment = upload_api_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    created = _assert_created_response(
        _post_upload(
            client,
            _export("public-status"),
            key="upload-public-status-request-0001",
        ),
        environment,
    )
    status_path = f"{UPLOAD_PATH}/{created['job_id']}"

    _use_token(client, None)
    assert client.get(status_path).status_code == 401
    _use_token(client, environment.alice_token)
    response = client.get(status_path)
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers.get("cache-control", "").casefold()
    payload = response.json()
    assert set(payload) == STATUS_FIELDS
    assert payload["job_id"] == created["job_id"]
    assert payload["state"] == "queued_parse"
    assert payload["job_version"] == 2
    assert payload["preview_version"] == 0
    assert payload["progress"] == {"completed": 0, "total": 0}
    assert payload["failure_code"] is None
    assert isinstance(payload["created_at"], str)
    assert isinstance(payload["updated_at"], str)
    assert payload["completed_at"] is None
    _assert_public_payload(payload, environment.data_directory)

    database_url = f"sqlite+aiosqlite:///{(environment.data_directory / 'main.sqlite3').as_posix()}"
    database = Database(database_url)

    async def set_internal_failure_code() -> None:
        now = utc_now()
        async with database.sessions() as session:
            changed = await session.execute(
                update(BookmarkImportJob)
                .where(
                    BookmarkImportJob.user_id == environment.alice_id,
                    BookmarkImportJob.id == created["job_id"],
                )
                .values(
                    state="failed",
                    failure_code="sqlite_bookmark_import_write_failed",
                    updated_at=now,
                    completed_at=now,
                )
            )
            assert changed.rowcount == 1
            await session.commit()

    try:
        asyncio.run(set_internal_failure_code())
    finally:
        asyncio.run(database.dispose())

    failed = client.get(status_path)
    assert failed.status_code == 200, failed.text
    assert failed.json()["failure_code"] == "internal_error"


@pytest.mark.skipif(
    not MOCK_EXPORT.is_file(),
    reason="本机未提供受 .gitignore 保护的书签 mock",
)
def test_real_chrome_edge_mock_export_is_accepted_without_buffer_contract_leaks(
    upload_api_environment: UploadApiEnvironment,
) -> None:
    environment = upload_api_environment
    client = environment.client
    _use_token(client, environment.alice_token)
    assert MOCK_EXPORT.stat().st_size == 1_601_123

    with MOCK_EXPORT.open("rb") as source:
        response = _post_upload(
            client,
            iter(lambda: source.read(64 * 1024), b""),
            key="upload-real-mock-request-0001",
            content_type="application/octet-stream",
        )

    created = _assert_created_response(response, environment)
    status = client.get(f"{UPLOAD_PATH}/{created['job_id']}")
    assert status.status_code == 200, status.text
    _assert_public_payload(status.json(), environment.data_directory)
