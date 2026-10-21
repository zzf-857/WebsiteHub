"""Table-driven tests for the pure resource-kind rules (``library.kinds``)."""

from __future__ import annotations

import json

import pytest

from webhub.library.kinds import (
    DEFAULT_KIND,
    SITE_KINDS,
    infer_kind,
    kind_columns,
    kind_meta,
    kind_meta_json,
    repo_meta,
)

KIND_CASES: list[tuple[str, str]] = [
    # Code forges: repositories, in-repository paths and item pages.
    ("https://github.com/langchain-ai/langgraph", "repo"),
    ("https://github.com/langchain-ai/langgraph/", "repo"),
    ("https://github.com/owner/repo.git", "repo"),
    ("https://github.com/owner/repo/tree/main/src", "repo"),
    ("https://github.com/owner/repo/blob/main/README.md", "repo"),
    ("https://github.com/owner/repo/releases/tag/v1.0", "repo"),
    ("https://github.com/owner/repo/issues", "repo"),
    ("https://github.com/org/.github", "repo"),
    ("https://www.github.com/Owner/Repo", "repo"),
    ("https://github.com/owner/repo/issues/12", "article"),
    ("https://github.com/owner/repo/pull/5/files", "article"),
    ("https://github.com/owner/repo/discussions/7", "article"),
    ("https://github.com/owner", "website"),
    ("https://github.com/topics/python", "website"),
    ("https://github.com/features/copilot", "website"),
    ("https://github.com/", "website"),
    ("https://gitlab.com/group/project", "repo"),
    ("https://gitlab.com/group/sub/project/-/tree/main", "repo"),
    ("https://gitlab.com/group/project/blob/main/README.md", "repo"),
    ("https://gitlab.com/group/sub/project/-/issues/3", "article"),
    ("https://gitlab.com/group/project/-/merge_requests/9", "article"),
    ("https://gitlab.com/explore/projects", "website"),
    ("https://gitee.com/mindspore/mindspore", "repo"),
    ("https://gitee.com/owner/repo/issues/I8ABC", "article"),
    ("https://codeberg.org/forgejo/forgejo", "repo"),
    ("https://codeberg.org/forgejo/forgejo/pulls/123", "article"),
    ("https://huggingface.co/meta-llama/Llama-3-8B", "repo"),
    ("https://huggingface.co/datasets/openai/gsm8k", "repo"),
    ("https://huggingface.co/spaces/owner/demo", "repo"),
    ("https://hf.co/owner/model", "repo"),
    ("https://huggingface.co/owner/model/discussions/2", "article"),
    ("https://huggingface.co/blog/some-post", "article"),
    ("https://huggingface.co/models", "website"),
    # Video hosts.
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "video"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=10", "video"),
    ("https://www.youtube.com/shorts/abc123", "video"),
    ("https://www.youtube.com/playlist?list=PL123", "video"),
    ("https://youtu.be/dQw4w9WgXcQ", "video"),
    ("https://www.youtube.com/watch", "website"),
    ("https://www.youtube.com/@channel/videos", "website"),
    ("https://www.youtube.com/post/Ugkx123", "article"),
    ("https://www.bilibili.com/video/BV1xx411c7mD", "video"),
    ("https://www.bilibili.com/bangumi/play/ep123", "video"),
    ("https://live.bilibili.com/22603245", "video"),
    ("https://b23.tv/AbCdEf", "video"),
    ("https://space.bilibili.com/123/video", "website"),
    ("https://www.bilibili.com/read/cv123456", "article"),
    ("https://vimeo.com/123456789", "video"),
    ("https://player.vimeo.com/video/123456789", "video"),
    ("https://vimeo.com/channels/staffpicks", "website"),
    # Posts and articles.
    ("https://example.com/t/some-topic/123", "article"),
    ("https://zhuanlan.zhihu.com/p/123456", "article"),
    ("https://example.com/post/hello", "article"),
    ("https://example.com/posts/hello", "article"),
    ("https://example.com/article/hello", "article"),
    ("https://example.com/articles/hello", "article"),
    ("https://example.com/blog/post-1", "article"),
    ("https://mp.weixin.qq.com/s/AbCdEfGh", "article"),
    ("https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=abc", "article"),
    ("https://www.zhihu.com/question/123", "article"),
    ("https://www.zhihu.com/question/123/answer/456", "article"),
    ("https://x.com/someone/status/1234567890", "article"),
    ("https://www.reddit.com/r/programming/comments/abc/title", "article"),
    ("https://news.ycombinator.com/item?id=41000000", "article"),
    ("https://example.com/papers/attention.pdf", "article"),
    ("https://example.com/paper.pdf", "article"),
    ("https://example.com/docs/getting-started", "article"),
    ("https://example.com/zh-cn/blog/post", "article"),
    ("https://example.com/how-to-cook-rice", "article"),
    ("https://medium.com/@user/a-post-title", "article"),
    ("https://example.com/?p=123", "article"),
    # Websites: roots, short single segments, listings and profiles.
    ("https://example.com", "website"),
    ("https://example.com/", "website"),
    ("https://example.com/about", "website"),
    ("https://example.com/about.html", "website"),
    ("https://example.com/getting-started", "website"),
    ("https://example.com/blog/", "website"),
    ("https://example.com/en/docs", "website"),
    ("https://example.com/tag/python", "website"),
    ("https://www.reddit.com/r/programming", "website"),
    ("https://www.zhihu.com/people/someone", "website"),
    ("https://news.ycombinator.com/newest", "website"),
    ("https://user.github.io/project/", "website"),
    ("http://localhost:3000/", "website"),
]

MALFORMED_INPUTS: list[object] = [
    None,
    "",
    "   ",
    123,
    b"https://github.com/owner/repo",
    "http://[::1",
    "https://",
    "not a url",
    "javascript:alert(1)",
    "https://exa mple.com/a b/c",
    "%%%",
    "https://github.com/%E4%B8%AD/%E6%96%87",
]


@pytest.mark.parametrize(("url", "expected"), KIND_CASES)
def test_infer_kind(url: str, expected: str) -> None:
    assert infer_kind(url) == expected


def test_case_table_covers_every_kind_with_enough_examples() -> None:
    assert len(KIND_CASES) >= 25
    assert {expected for _, expected in KIND_CASES} == set(SITE_KINDS)
    assert len({url for url, _ in KIND_CASES}) == len(KIND_CASES)


@pytest.mark.parametrize("value", MALFORMED_INPUTS)
def test_malformed_input_never_raises(value: object) -> None:
    assert infer_kind(value) in SITE_KINDS  # type: ignore[arg-type]
    assert repo_meta(value) is None or isinstance(repo_meta(value), dict)  # type: ignore[arg-type]
    kind, meta_json = kind_columns(value)  # type: ignore[arg-type]
    assert kind in SITE_KINDS
    assert isinstance(json.loads(meta_json), dict)


def test_unparseable_input_falls_back_to_the_default_kind() -> None:
    for value in (None, "", "   ", 123, "http://[::1"):
        assert infer_kind(value) == DEFAULT_KIND  # type: ignore[arg-type]
        assert repo_meta(value) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/langchain-ai/langgraph/tree/main/libs",
            {"host": "github.com", "owner": "langchain-ai", "repo": "langgraph"},
        ),
        (
            "https://github.com/owner/repo.git",
            {"host": "github.com", "owner": "owner", "repo": "repo"},
        ),
        (
            "https://gitlab.com/group/sub/project/-/issues/3",
            {"host": "gitlab.com", "owner": "group/sub", "repo": "project"},
        ),
        (
            "https://hf.co/datasets/openai/gsm8k",
            {
                "host": "huggingface.co",
                "owner": "openai",
                "repo": "gsm8k",
                "repo_type": "dataset",
            },
        ),
        (
            "https://codeberg.org/forgejo/forgejo",
            {"host": "codeberg.org", "owner": "forgejo", "repo": "forgejo"},
        ),
        ("https://github.com/topics/python", None),
        ("https://github.com/owner", None),
        ("https://example.com/owner/repo", None),
        ("https://www.youtube.com/watch?v=abc", None),
    ],
)
def test_repo_meta(url: str, expected: dict[str, str] | None) -> None:
    assert repo_meta(url) == expected


def test_only_repositories_store_url_metadata() -> None:
    issue = "https://github.com/owner/repo/issues/12"
    repository = "https://github.com/owner/repo"
    # An issue still parses to its repository; only the repo kind stores it.
    assert repo_meta(issue) == {"host": "github.com", "owner": "owner", "repo": "repo"}
    assert kind_meta("article", issue) == {}
    assert kind_meta("repo", issue) == {"host": "github.com", "owner": "owner", "repo": "repo"}
    assert kind_meta("repo", "https://example.com/") == {}
    assert kind_columns(repository) == (
        "repo",
        '{"host":"github.com","owner":"owner","repo":"repo"}',
    )
    assert kind_columns(issue) == ("article", "{}")
    assert kind_columns("https://youtu.be/abc") == ("video", "{}")


def test_meta_json_is_canonical() -> None:
    first = kind_meta_json("repo", "https://huggingface.co/spaces/Owner/Demo")
    second = kind_meta_json("repo", "https://www.huggingface.co/spaces/Owner/Demo/")
    assert first == second
    assert first == (
        '{"host":"huggingface.co","owner":"Owner","repo":"Demo","repo_type":"space"}'
    )
    assert kind_meta_json("repo", "https://gitee.com/中文/仓库") == "{}"
