from pathlib import Path

from alembic.config import Config

from alembic import command
from webhub.db.preflight import assert_upgrade_safe

API_ROOT = Path(__file__).resolve().parents[3]


def create_alembic_config(database_url: str) -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


def upgrade_database(database_url: str) -> None:
    assert_upgrade_safe(database_url)
    command.upgrade(create_alembic_config(database_url), "head")
