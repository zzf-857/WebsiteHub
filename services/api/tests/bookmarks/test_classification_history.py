import asyncio
from pathlib import Path

from webhub.bookmarks.classification_history import (
    HostCategoryRecord,
    build_host_category_history,
    load_account_host_category_history,
)
from webhub.db.database import Database
from webhub.db.migrations import upgrade_database
from webhub.db.models import Category, Site, SiteMetadataPreference, User, new_id


def _record(
    url_or_host: str,
    category: str,
    *,
    default: bool = False,
    manual: bool = False,
    llm: bool = False,
) -> HostCategoryRecord:
    return HostCategoryRecord(url_or_host, category, default, manual, llm)


def test_manual_history_is_reused_across_www_but_shared_hosts_are_excluded() -> None:
    history = build_host_category_history(
        [
            _record("https://www.example.com/docs", "学习与文档", manual=True),
            _record("https://github.com/acme/project", "AI 与 Agent", manual=True),
        ]
    )

    assert history["example.com"].category == "学习与文档"
    assert history["example.com"].confidence == "high"
    assert "github.com" not in history


def test_history_uses_similarity_site_keys_and_keeps_non_default_ports_separate() -> None:
    history = build_host_category_history(
        [
            _record("https://www.example.com:8443/docs", "学习与文档", manual=True),
            _record("https://example.com:9443/api", "开发与技术", manual=True),
        ]
    )

    assert history["example.com:8443"].category == "学习与文档"
    assert history["example.com:9443"].category == "开发与技术"
    assert "example.com" not in history


def test_shared_platform_exclusion_cannot_be_bypassed_with_subdomain_or_port() -> None:
    history = build_host_category_history(
        [
            _record("https://www.github.com:8443/acme/project", "AI 与 Agent", manual=True),
            _record("https://team.github.io/docs", "学习与文档", manual=True),
            _record("https://mail.google.com/inbox", "办公与协作", manual=True),
        ]
    )

    assert history == {}


def test_automatic_history_requires_repetition_and_high_coverage() -> None:
    one = build_host_category_history([_record("example.com/a", "开发与技术")])
    repeated = build_host_category_history(
        [
            _record("example.com/a", "开发与技术"),
            _record("www.example.com/b", "开发与技术"),
        ]
    )
    diluted = build_host_category_history(
        [
            _record("example.com/a", "开发与技术"),
            _record("example.com/b", "开发与技术"),
            _record("example.com/c", "未分类", default=True),
        ]
    )

    assert one == {}
    assert repeated["example.com"].evidence == ("history:consistent:2/2",)
    assert diluted == {}


def test_conflicting_manual_history_is_explicitly_ambiguous() -> None:
    history = build_host_category_history(
        [
            _record("example.com/a", "开发与技术", manual=True),
            _record("www.example.com/b", "AI 与 Agent", manual=True),
        ]
    )

    assert history["example.com"].category == "未分类"
    assert history["example.com"].confidence == "ambiguous"


def test_agreeing_manual_history_overrides_lower_priority_conflicts() -> None:
    history = build_host_category_history(
        [
            _record("example.com/a", "AI 与 Agent", manual=True),
            _record("example.com/b", "AI 与 Agent", manual=True),
            _record("example.com/old", "开发与技术", llm=True),
        ]
    )

    assert history["example.com"].category == "AI 与 Agent"
    assert history["example.com"].evidence == ("history:manual:2",)


def test_llm_and_automatic_conflict_falls_back_to_ambiguous() -> None:
    history = build_host_category_history(
        [
            _record("example.com/a", "AI 与 Agent", llm=True),
            _record("example.com/b", "开发与技术"),
        ]
    )

    assert history["example.com"].category == "未分类"
    assert history["example.com"].confidence == "ambiguous"


def test_one_existing_llm_decision_can_be_reused_without_another_model_call() -> None:
    history = build_host_category_history(
        [_record("example.com/a", "设计与创作", llm=True)]
    )

    assert history["example.com"].category == "设计与创作"
    assert history["example.com"].evidence == ("history:llm:1",)


def test_history_query_is_account_scoped(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'history.sqlite3').as_posix()}"
    upgrade_database(database_url)

    async def exercise() -> None:
        database = Database(database_url)
        try:
            async with database.sessions() as session:
                alice_id = new_id()
                bob_id = new_id()
                alice_category_id = new_id()
                bob_category_id = new_id()
                alice_site_id = new_id()
                bob_site_id = new_id()
                session.add_all(
                    [
                        User(
                            id=alice_id,
                            username="alice-history",
                            display_name="Alice",
                            password_hash="not-used",
                        ),
                        User(
                            id=bob_id,
                            username="bob-history",
                            display_name="Bob",
                            password_hash="not-used",
                        ),
                        Category(
                            id=alice_category_id,
                            user_id=alice_id,
                            name="开发与技术",
                            normalized_name="开发与技术",
                        ),
                        Category(
                            id=bob_category_id,
                            user_id=bob_id,
                            name="AI 与 Agent",
                            normalized_name="ai 与 agent",
                        ),
                        Site(
                            id=alice_site_id,
                            user_id=alice_id,
                            category_id=alice_category_id,
                            name="Alice site",
                            normalized_name="alice site",
                            original_url="https://example.com/a",
                            identity_url="https://example.com/a",
                            position=0,
                            source="manual",
                        ),
                        Site(
                            id=bob_site_id,
                            user_id=bob_id,
                            category_id=bob_category_id,
                            name="Bob site",
                            normalized_name="bob site",
                            original_url="https://example.com/b",
                            identity_url="https://example.com/b",
                            position=0,
                            source="manual",
                        ),
                        SiteMetadataPreference(
                            user_id=alice_id,
                            site_id=alice_site_id,
                            category_is_manual=True,
                        ),
                        SiteMetadataPreference(
                            user_id=bob_id,
                            site_id=bob_site_id,
                            category_is_manual=True,
                        ),
                    ]
                )
                await session.commit()

                history = await load_account_host_category_history(session, alice_id)

            assert history["example.com"].category == "开发与技术"
            assert history["example.com"].confidence == "high"
        finally:
            await database.dispose()

    asyncio.run(exercise())
