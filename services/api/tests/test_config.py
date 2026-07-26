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
