from __future__ import annotations

from webhub.search.fusion import Candidate, exact_site_ids, fuse, normalize_for_match

CANDIDATES = [
    Candidate("github", "GitHub", "https://github.com"),
    Candidate("tutorial", "GitHub 入门教程", "https://example.com/github"),
    Candidate("gitee", "Gitee", "https://gitee.com"),
    Candidate("gitlab", "GitLab", "https://gitlab.com"),
]


def test_an_exact_name_wins_even_when_it_scores_lower() -> None:
    """The queue's headline criterion, stated as the thing that could break it.

    Someone typing a site's real name knows what they want; semantic recall
    must not bury it under things that are merely related.
    """

    # 'gitee' is ranked first by both lists, so RRF alone would put it on top.
    ranked = fuse("github", ["gitee", "github", "tutorial"], ["gitee", "github"], CANDIDATES)

    assert ranked[0].site_id == "github"
    assert ranked[0].exact is True
    # And it genuinely was the lower-scoring one — the promotion is doing work.
    assert ranked[0].score < ranked[1].score


def test_a_substring_is_a_keyword_hit_not_an_exact_one() -> None:
    """'git' inside 'GitHub' must not hijack the top slot."""

    assert exact_site_ids("git", CANDIDATES) == set()
    ranked = fuse("git", ["tutorial", "github"], [], CANDIDATES)
    assert ranked[0].site_id == "tutorial"
    assert all(not hit.exact for hit in ranked)


def test_a_url_matches_with_or_without_the_scheme() -> None:
    # Users rarely type the protocol.
    assert exact_site_ids("github.com", CANDIDATES) == {"github"}
    assert exact_site_ids("https://github.com", CANDIDATES) == {"github"}
    assert exact_site_ids("gitee.com", CANDIDATES) == {"gitee"}


def test_matching_folds_width_case_and_whitespace() -> None:
    assert normalize_for_match("  ＧｉｔＨｕｂ  ") == "github"
    assert exact_site_ids("  ＧｉｔＨｕｂ ", CANDIDATES) == {"github"}


def test_semantic_only_hits_still_surface() -> None:
    """Recall is the point of adding vectors; keyword-only would drop these."""

    ranked = fuse("代码托管", [], ["gitee", "gitlab"], CANDIDATES)
    assert [hit.site_id for hit in ranked] == ["gitee", "gitlab"]
    assert all(hit.sources == ("semantic",) for hit in ranked)


def test_appearing_in_both_lists_outranks_appearing_in_one() -> None:
    ranked = fuse("托管", ["gitlab", "gitee"], ["gitee"], CANDIDATES)
    assert ranked[0].site_id == "gitee"
    assert ranked[0].sources == ("keyword", "semantic")


def test_ranking_is_deterministic_for_equal_scores() -> None:
    """A search that reshuffles equal rows between refreshes reads as broken."""

    first = fuse("托管", ["gitee", "gitlab"], [], CANDIDATES)
    second = fuse("托管", ["gitee", "gitlab"], [], CANDIDATES)
    assert [hit.site_id for hit in first] == [hit.site_id for hit in second]
    # Tie broken by the keyword list's own order, not by set iteration order.
    assert [hit.site_id for hit in first] == ["gitee", "gitlab"]


def test_an_empty_semantic_list_degrades_to_pure_keyword_order() -> None:
    """No embedding Provider配置 means semantic_ids is empty — not an error."""

    ranked = fuse("托管", ["gitee", "gitlab", "github"], [], CANDIDATES)
    assert [hit.site_id for hit in ranked] == ["gitee", "gitlab", "github"]
    assert all(hit.semantic_rank is None for hit in ranked)


def test_both_lists_empty_yields_nothing_rather_than_failing() -> None:
    assert fuse("查无此站", [], [], CANDIDATES) == []


def test_an_empty_query_matches_nothing_exactly() -> None:
    # Otherwise a blank search would promote every site with an empty name.
    assert exact_site_ids("   ", CANDIDATES) == set()
