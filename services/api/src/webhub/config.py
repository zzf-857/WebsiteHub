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


@lru_cache
def get_settings() -> Settings:
    environment = getenv("WEBHUB_ENVIRONMENT", "development")
    return Settings(
        environment=environment,
        session_cookie_secure=_environment_flag("WEBHUB_SESSION_COOKIE_SECURE", False),
    )
