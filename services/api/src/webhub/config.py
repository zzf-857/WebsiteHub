import base64
import binascii
import secrets
from contextlib import suppress
from dataclasses import dataclass, field, replace
from functools import lru_cache
from os import getenv
from pathlib import Path

from dotenv import load_dotenv

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]

# ``.env.example`` documents a dozen WEBHUB_* variables and ``.gitignore``
# excludes ``.env``, so the file is clearly meant to be read — but nothing was
# reading it, which made every documented setting silently inert.  Load it once
# at import, without overriding anything the real environment already set.
load_dotenv(REPOSITORY_ROOT / ".env", override=False)

# Where a development-only Provider master key is persisted after being
# generated on first use.  Kept beside the database because losing one without
# the other is useless anyway: the key decrypts secrets that only live there.
DEVELOPMENT_MASTER_KEY_FILENAME = "provider-master.key"


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


def _decoded_master_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(
            value.strip().encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ValueError(
            "WEBHUB_PROVIDER_MASTER_KEY must be a base64-encoded 32-byte key"
        ) from error
    if len(decoded) != 32:
        raise ValueError(
            "WEBHUB_PROVIDER_MASTER_KEY must be a base64-encoded 32-byte key"
        )
    return decoded


def development_master_key(data_directory: Path) -> bytes:
    """Read — or on first use create — the local development master key.

    Without a key the whole Provider feature is dead on arrival: saving an API
    key fails with a 503 that reads "contact your service administrator", which
    is meaningless advice for a single-user app the user is hosting themselves.
    Generating one on first run trades a little secrecy for a feature that
    actually works.

    The trade is explicit, and bounded to development:

    * the key sits next to ``main.sqlite3``, so whoever can read the database
      can also read the key.  That is already true of a local desktop install,
      and it is strictly better than storing the API keys in plaintext or not
      storing them at all;
    * production still refuses to start without ``WEBHUB_PROVIDER_MASTER_KEY``
      (see ``Settings.__post_init__``), so this path can never be reached by a
      deployment that has real users;
    * the file is created with owner-only permissions where the platform
      supports it, and it must be backed up — losing it makes every stored API
      key permanently undecryptable.
    """

    key_path = data_directory / DEVELOPMENT_MASTER_KEY_FILENAME
    if key_path.exists():
        return _decoded_master_key(key_path.read_text(encoding="ascii"))

    data_directory.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_bytes(32)
    encoded = base64.b64encode(generated).decode("ascii")
    # Exclusive create: two workers starting at once must not race to write
    # different keys and leave half the secrets undecryptable.
    try:
        with open(key_path, "x", encoding="ascii") as handle:
            handle.write(encoded)
    except FileExistsError:
        return _decoded_master_key(key_path.read_text(encoding="ascii"))
    # Windows ACLs do not map onto POSIX modes; the file still inherits the
    # directory's protection, so a failure here is not worth blocking startup.
    with suppress(OSError):
        key_path.chmod(0o600)
    return generated


def _provider_master_key() -> bytes | None:
    value = getenv("WEBHUB_PROVIDER_MASTER_KEY")
    if value is None or not value.strip():
        return None
    return _decoded_master_key(value)


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
    provider_master_key: bytes | None = field(
        default_factory=_provider_master_key,
        repr=False,
    )
    provider_master_key_version: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_PROVIDER_MASTER_KEY_VERSION", 1
        )
    )
    provider_test_rate_limit_attempts: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_PROVIDER_TEST_RATE_LIMIT_ATTEMPTS", 10
        )
    )
    provider_test_rate_limit_window_seconds: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_PROVIDER_TEST_RATE_LIMIT_WINDOW_SECONDS", 60
        )
    )
    provider_test_max_tracked_accounts: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_PROVIDER_TEST_MAX_TRACKED_ACCOUNTS", 10_000
        )
    )
    provider_test_timeout_seconds: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_PROVIDER_TEST_TIMEOUT_SECONDS", 3
        )
    )
    agent_request_timeout_seconds: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_AGENT_REQUEST_TIMEOUT_SECONDS", 60
        )
    )
    agent_max_steps: int = field(
        default_factory=lambda: _positive_environment_integer("WEBHUB_AGENT_MAX_STEPS", 12)
    )
    agent_history_messages: int = field(
        default_factory=lambda: _positive_environment_integer(
            "WEBHUB_AGENT_HISTORY_MESSAGES", 20
        )
    )
    agent_tool_result_limit: int = field(
        default_factory=lambda: _positive_environment_integer("WEBHUB_AGENT_TOOL_RESULT_LIMIT", 8)
    )
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
            "provider_master_key_version": self.provider_master_key_version,
            "provider_test_rate_limit_attempts": self.provider_test_rate_limit_attempts,
            "provider_test_rate_limit_window_seconds": (
                self.provider_test_rate_limit_window_seconds
            ),
            "provider_test_max_tracked_accounts": (
                self.provider_test_max_tracked_accounts
            ),
            "provider_test_timeout_seconds": self.provider_test_timeout_seconds,
            "agent_request_timeout_seconds": self.agent_request_timeout_seconds,
            "agent_max_steps": self.agent_max_steps,
            "agent_history_messages": self.agent_history_messages,
            "agent_tool_result_limit": self.agent_tool_result_limit,
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
        if self.provider_master_key is not None and (
            not isinstance(self.provider_master_key, bytes)
            or len(self.provider_master_key) != 32
        ):
            raise ValueError("provider_master_key must contain exactly 32 bytes")
        if (
            self.environment.strip().casefold() in {"prod", "production"}
            and self.provider_master_key is None
        ):
            raise ValueError(
                "WEBHUB_PROVIDER_MASTER_KEY is required in production"
            )
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
    settings = Settings(
        environment=environment,
        session_cookie_secure=_environment_flag("WEBHUB_SESSION_COOKIE_SECURE", False),
    )
    if settings.provider_master_key is not None:
        return settings
    # Development only: production already refused to construct above.
    return replace(
        settings,
        provider_master_key=development_master_key(settings.data_directory),
    )
