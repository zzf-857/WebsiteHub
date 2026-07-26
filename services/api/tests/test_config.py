import base64
from pathlib import Path

import pytest

from webhub.bookmarks.admission import BookmarkUploadAdmissionManager
from webhub.config import REPOSITORY_ROOT, Settings, get_settings
from webhub.main import create_app

_UPLOAD_ENVIRONMENT_VARIABLES = (
    "WEBHUB_BOOKMARK_UPLOAD_GLOBAL_CONCURRENCY",
    "WEBHUB_BOOKMARK_UPLOAD_RATE_LIMIT_ATTEMPTS",
    "WEBHUB_BOOKMARK_UPLOAD_RATE_LIMIT_WINDOW_SECONDS",
    "WEBHUB_BOOKMARK_UPLOAD_MAX_TRACKED_ACCOUNTS",
    "WEBHUB_BOOKMARK_UPLOAD_ACCOUNT_QUOTA_BYTES",
    "WEBHUB_BOOKMARK_UPLOAD_MINIMUM_FREE_BYTES",
    "WEBHUB_BOOKMARK_UPLOAD_DISK_CHECK_INTERVAL_BYTES",
)


def _clear_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "WEBHUB_PROVIDER_MASTER_KEY",
        "WEBHUB_PROVIDER_MASTER_KEY_VERSION",
        "WEBHUB_PROVIDER_TEST_RATE_LIMIT_ATTEMPTS",
        "WEBHUB_PROVIDER_TEST_RATE_LIMIT_WINDOW_SECONDS",
        "WEBHUB_PROVIDER_TEST_MAX_TRACKED_ACCOUNTS",
        "WEBHUB_PROVIDER_TEST_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def _clear_upload_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _UPLOAD_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_relative_data_directory_is_anchored_to_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBHUB_DATA_DIR", "relative-data-test")
    get_settings.cache_clear()
    try:
        assert get_settings().data_directory == (REPOSITORY_ROOT / "relative-data-test").resolve()
    finally:
        get_settings.cache_clear()


def test_bookmark_upload_settings_have_conservative_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_upload_environment(monkeypatch)
    settings = Settings(environment="test")

    assert settings.bookmark_upload_global_concurrency == 4
    assert settings.bookmark_upload_rate_limit_attempts == 6
    assert settings.bookmark_upload_rate_limit_window_seconds == 60
    assert settings.bookmark_upload_max_tracked_accounts == 10_000
    assert settings.bookmark_upload_account_quota_bytes == 2 * 1024 * 1024 * 1024
    assert settings.bookmark_upload_minimum_free_bytes == 512 * 1024 * 1024
    assert settings.bookmark_upload_disk_check_interval_bytes == 8 * 1024 * 1024


def test_bookmark_upload_settings_can_be_overridden_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "WEBHUB_BOOKMARK_UPLOAD_GLOBAL_CONCURRENCY": "3",
        "WEBHUB_BOOKMARK_UPLOAD_RATE_LIMIT_ATTEMPTS": "9",
        "WEBHUB_BOOKMARK_UPLOAD_RATE_LIMIT_WINDOW_SECONDS": "30",
        "WEBHUB_BOOKMARK_UPLOAD_MAX_TRACKED_ACCOUNTS": "12",
        "WEBHUB_BOOKMARK_UPLOAD_ACCOUNT_QUOTA_BYTES": "1000",
        "WEBHUB_BOOKMARK_UPLOAD_MINIMUM_FREE_BYTES": "200",
        "WEBHUB_BOOKMARK_UPLOAD_DISK_CHECK_INTERVAL_BYTES": "20",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.bookmark_upload_global_concurrency == 3
        assert settings.bookmark_upload_rate_limit_attempts == 9
        assert settings.bookmark_upload_rate_limit_window_seconds == 30
        assert settings.bookmark_upload_max_tracked_accounts == 12
        assert settings.bookmark_upload_account_quota_bytes == 1000
        assert settings.bookmark_upload_minimum_free_bytes == 200
        assert settings.bookmark_upload_disk_check_interval_bytes == 20
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("value", ["0", "-1", "true", "1.5", "invalid"])
def test_bookmark_upload_environment_values_must_be_positive_integers(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    _clear_upload_environment(monkeypatch)
    name = "WEBHUB_BOOKMARK_UPLOAD_GLOBAL_CONCURRENCY"
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match=name):
            get_settings()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("value", [0, -1, True])
def test_programmatic_bookmark_upload_limits_must_be_positive_integers(
    monkeypatch: pytest.MonkeyPatch,
    value: int,
) -> None:
    _clear_upload_environment(monkeypatch)
    with pytest.raises(ValueError, match="bookmark_upload_global_concurrency"):
        Settings(environment="test", bookmark_upload_global_concurrency=value)


def test_bookmark_upload_global_concurrency_cannot_exceed_state_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_upload_environment(monkeypatch)
    with pytest.raises(ValueError, match="cannot exceed"):
        Settings(
            environment="test",
            bookmark_upload_global_concurrency=3,
            bookmark_upload_max_tracked_accounts=2,
        )


def test_bookmark_upload_disk_reserve_covers_all_concurrent_check_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_upload_environment(monkeypatch)
    with pytest.raises(ValueError, match="must be at least"):
        Settings(
            environment="test",
            bookmark_upload_global_concurrency=3,
            bookmark_upload_minimum_free_bytes=29,
            bookmark_upload_disk_check_interval_bytes=10,
        )

    settings = Settings(
        environment="test",
        bookmark_upload_global_concurrency=3,
        bookmark_upload_minimum_free_bytes=30,
        bookmark_upload_disk_check_interval_bytes=10,
    )
    assert settings.bookmark_upload_minimum_free_bytes == 30


def test_create_app_registers_configured_bookmark_upload_admission(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'main.sqlite3').as_posix()}",
        data_directory=tmp_path,
        bookmark_upload_global_concurrency=3,
        bookmark_upload_rate_limit_attempts=8,
        bookmark_upload_rate_limit_window_seconds=45,
        bookmark_upload_max_tracked_accounts=20,
        bookmark_upload_account_quota_bytes=4_000,
        bookmark_upload_minimum_free_bytes=500,
        bookmark_upload_disk_check_interval_bytes=50,
    )

    application = create_app(settings=settings)
    manager = application.state.bookmark_upload_admission

    assert isinstance(manager, BookmarkUploadAdmissionManager)
    assert application.state.settings is settings
    assert manager.data_directory == tmp_path.resolve()
    assert manager.global_concurrency == 3
    assert manager.rate_limit_attempts == 8
    assert manager.rate_limit_window_seconds == 45
    assert manager.max_tracked_accounts == 20
    assert manager.account_quota_bytes == 4_000
    assert manager.minimum_free_bytes == 500
    assert manager.disk_check_interval_bytes == 50


def test_provider_master_key_is_decoded_and_hidden_from_settings_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_environment(monkeypatch)
    raw_key = b"provider-config-test-key-32bytes"
    assert len(raw_key) == 32
    encoded = base64.urlsafe_b64encode(raw_key).decode()
    monkeypatch.setenv("WEBHUB_PROVIDER_MASTER_KEY", encoded)
    monkeypatch.setenv("WEBHUB_PROVIDER_MASTER_KEY_VERSION", "7")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.provider_master_key == raw_key
        assert settings.provider_master_key_version == 7
        assert encoded not in repr(settings)
        assert raw_key.hex() not in repr(settings)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("value", ["not-base64!", "c2hvcnQ=", ""])
def test_invalid_provider_master_key_fails_closed_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    _clear_provider_environment(monkeypatch)
    monkeypatch.setenv("WEBHUB_PROVIDER_MASTER_KEY", value)
    get_settings.cache_clear()
    try:
        if value:
            with pytest.raises(ValueError, match="base64-encoded 32-byte"):
                get_settings()
        else:
            assert get_settings().provider_master_key is None
    finally:
        get_settings.cache_clear()


def test_production_requires_provider_master_key_but_development_degrades_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_environment(monkeypatch)
    assert Settings(environment="development", provider_master_key=None).provider_master_key is None
    with pytest.raises(ValueError, match="required in production"):
        Settings(environment="production", provider_master_key=None)


@pytest.mark.parametrize("value", [0, -1, True])
def test_provider_request_budget_settings_must_be_positive(value: int) -> None:
    with pytest.raises(ValueError, match="provider_test_rate_limit_attempts"):
        Settings(
            environment="test",
            provider_test_rate_limit_attempts=value,
        )
