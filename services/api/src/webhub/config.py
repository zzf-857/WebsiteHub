from dataclasses import dataclass, field
from functools import lru_cache
from os import getenv
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _environment_flag(name: str, default: bool) -> bool:
    value = getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _positive_environment_integer(name: str, default: int) -> int:
    value = getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _default_data_directory() -> Path:
    configured = getenv("WEBHUB_DATA_DIR")
    if not configured:
        return REPOSITORY_ROOT / ".data"
    configured_path = Path(configured).expanduser()
    if not configured_path.is_absolute():
        configured_path = REPOSITORY_ROOT / configured_path
    return configured_path.resolve()


def _default_database_url() -> str:
    configured = getenv("WEBHUB_DATABASE_URL")
    if configured:
        return configured
    database_path = (_default_data_directory() / "main.sqlite3").as_posix()
    return f"sqlite+aiosqlite:///{database_path}"


def _allowed_origins() -> tuple[str, ...]:
    configured = getenv("WEBHUB_ALLOWED_ORIGINS", "")
    return tuple(origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    service_name: str = "webhub-api"
    service_version: str = "0.1.0"
    database_url: str = field(default_factory=_default_database_url)
    data_directory: Path = field(default_factory=_default_data_directory)
    session_cookie_name: str = "webhub_session"
    session_ttl_seconds: int = 60 * 60 * 24 * 30
    session_cookie_secure: bool = False
    allowed_origins: tuple[str, ...] = field(default_factory=_allowed_origins)
    bookmark_upload_global_concurrency: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_BOOKMARK_UPLOAD_GLOBAL_CONCURRENCY", 4
        )
    )
    bookmark_upload_rate_limit_attempts: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_BOOKMARK_UPLOAD_RATE_LIMIT_ATTEMPTS", 6
        )
    )
    bookmark_upload_rate_limit_window_seconds: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_BOOKMARK_UPLOAD_RATE_LIMIT_WINDOW_SECONDS", 60
        )
    )
    bookmark_upload_max_tracked_accounts: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_BOOKMARK_UPLOAD_MAX_TRACKED_ACCOUNTS", 10_000
        )
    )
    bookmark_upload_account_quota_bytes: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_BOOKMARK_UPLOAD_ACCOUNT_QUOTA_BYTES", 2 * 1024 * 1024 * 1024
        )
    )
    bookmark_upload_minimum_free_bytes: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_BOOKMARK_UPLOAD_MINIMUM_FREE_BYTES", 512 * 1024 * 1024
        )
    )
    bookmark_upload_disk_check_interval_bytes: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_BOOKMARK_UPLOAD_DISK_CHECK_INTERVAL_BYTES", 8 * 1024 * 1024
        )
    )

    def __post_init__(self) -> None:
        limits = {
            "bookmark_upload_global_concurrency": self.bookmark_upload_global_concurrency,
            "bookmark_upload_rate_limit_attempts": self.bookmark_upload_rate_limit_attempts,
            "bookmark_upload_rate_limit_window_seconds": (
                self.bookmark_upload_rate_limit_window_seconds
            ),
            "bookmark_upload_max_tracked_accounts": self.bookmark_upload_max_tracked_accounts,
            "bookmark_upload_account_quota_bytes": self.bookmark_upload_account_quota_bytes,
            "bookmark_upload_minimum_free_bytes": self.bookmark_upload_minimum_free_bytes,
            "bookmark_upload_disk_check_interval_bytes": (
                self.bookmark_upload_disk_check_interval_bytes
            ),
        }
        for name, value in limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.bookmark_upload_global_concurrency > self.bookmark_upload_max_tracked_accounts:
            raise ValueError(
                "bookmark_upload_global_concurrency cannot exceed "
                "bookmark_upload_max_tracked_accounts"
            )
        minimum_disk_reserve = (
            self.bookmark_upload_global_concurrency
            * self.bookmark_upload_disk_check_interval_bytes
        )
        if self.bookmark_upload_minimum_free_bytes < minimum_disk_reserve:
            raise ValueError(
                "bookmark_upload_minimum_free_bytes must be at least "
                "bookmark_upload_global_concurrency * "
                "bookmark_upload_disk_check_interval_bytes"
            )


@lru_cache
def get_settings() -> Settings:
    environment = getenv("WEBHUB_ENVIRONMENT", "development")
    return Settings(
        environment=environment,
        session_cookie_secure=_environment_flag("WEBHUB_SESSION_COOKIE_SECURE", False),
    )
