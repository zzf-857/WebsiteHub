from webhub.bookmarks.classification import (
    combine_category_suggestions,
    suggest_category,
)
from webhub.bookmarks.models import CategorySuggestion


def test_specific_folder_and_page_evidence_outweighs_a_shared_platform() -> None:
    suggestion = suggest_category(
        ("Bookmarks bar", "AI 工具"),
        "Claude prompts",
        "github.com",
        "/anthropic/prompt",
    )

    assert suggestion.category == "AI 与 Agent"
    assert suggestion.confidence == "high"
    assert "folder:ai" in suggestion.evidence
    assert "path:prompt" in suggestion.evidence


def test_conflicting_strong_folder_evidence_falls_back_to_uncategorized() -> None:
    suggestion = suggest_category(("设计", "开发"), "", "example.com", "/")

    assert suggestion.category == "未分类"
    assert suggestion.confidence == "ambiguous"
    assert any(item.startswith("conflict:") for item in suggestion.evidence)


def test_path_can_classify_when_folder_and_host_are_uninformative() -> None:
    suggestion = suggest_category((), "Reference", "example.com", "/api/python/sdk")

    assert suggestion.category == "开发与技术"
    assert suggestion.confidence == "medium"
    assert suggestion.evidence == ("path:api", "path:python", "path:sdk")


def test_latin_keywords_use_boundaries_instead_of_arbitrary_substrings() -> None:
    suggestion = suggest_category((), "Device portal", "device.example.com", "/")

    assert suggestion == CategorySuggestion("未分类", "none", ())


def test_known_host_rules_cover_more_precise_taxonomy() -> None:
    suggestion = suggest_category((), "Markets", "finance.yahoo.com", "/quote")

    assert suggestion.category == "商业与金融"
    assert suggestion.confidence == "high"


def test_history_fills_no_evidence_and_reinforces_agreement() -> None:
    history = CategorySuggestion("开发与技术", "high", ("history:manual:1",))

    filled = combine_category_suggestions(CategorySuggestion("未分类", "none", ()), history)
    agreed = combine_category_suggestions(
        CategorySuggestion("开发与技术", "medium", ("path:python",)),
        history,
    )

    assert filled == history
    assert agreed.category == "开发与技术"
    assert agreed.confidence == "high"


def test_history_never_overrides_conflicting_or_ambiguous_rules() -> None:
    history = CategorySuggestion("AI 与 Agent", "high", ("history:manual:1",))
    conflict = combine_category_suggestions(
        CategorySuggestion("开发与技术", "high", ("folder:开发",)),
        history,
    )
    ambiguous = CategorySuggestion("未分类", "ambiguous", ("conflict:设计与创作",))

    assert conflict.category == "未分类"
    assert conflict.confidence == "ambiguous"
    assert combine_category_suggestions(ambiguous, history) == ambiguous
