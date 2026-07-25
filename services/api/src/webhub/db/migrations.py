from pathlib import Path

from alembic.config import Config

from alembic import command

API_ROOT = Path(__file__).resolve().parents[3]


def create_alembic_config(database_url: str) -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


def upgrade_database(database_url: str) -> None:
    command.upgrade(create_alembic_config(database_url), "head")
