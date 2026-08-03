from __future__ import annotations

import re
from collections import defaultdict
from functools import cache

from webhub.bookmarks.models import CategorySuggestion

_BROWSER_ROOT_FOLDERS = {
    "bookmarks",
    "bookmarks bar",
    "bookmarks toolbar",
    "favorites bar",
    "mobile bookmarks",
    "other bookmarks",
    "书签",
    "书签栏",
    "收藏夹栏",
    "其他书签",
    "移动设备书签",
}
CLASSIFICATION_RULESET_VERSION = "bookmark-category-rules.v3"

# These are semantic hints, not an exhaustive web directory. A rule only wins
# when its evidence is sufficiently separated from the runner-up; otherwise the
# candidate remains uncategorized for review.
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "AI 与 Agent": (
        "ai",
        "agent",
        "agents",
        "artificial intelligence",
        "chatbot",
        "chatgpt",
        "claude",
        "copilot",
        "deepseek",
        "embedding",
        "generative ai",
        "gemini",
        "gpt",
        "hugging face",
        "huggingface",
        "llama",
        "llm",
        "machine learning",
        "midjourney",
        "ollama",
        "openai",
        "prompt",
        "rag",
        "stable diffusion",
        "transformer",
        "人工智能",
        "大模型",
        "机器学习",
        "生成式",
        "智能体",
    ),
    "开发与技术": (
        "api",
        "backend",
        "code",
        "coding",
        "compiler",
        "database",
        "developer",
        "frontend",
        "git",
        "github",
        "gitlab",
        "java",
        "javascript",
        "node.js",
        "npm",
        "programming",
        "python",
        "react",
        "repository",
        "rust",
        "sdk",
        "source code",
        "stackoverflow",
        "typescript",
        "unity",
        "vercel",
        "vue",
        "代码",
        "前端",
        "后端",
        "开发",
        "数据库",
        "编程",
        "源码",
    ),
    "学习与文档": (
        "academic",
        "arxiv",
        "book",
        "course",
        "docs",
        "documentation",
        "learn",
        "paper",
        "reference",
        "research",
        "tutorial",
        "wiki",
        "书籍",
        "学习",
        "教程",
        "文档",
        "论文",
        "课程",
        "资料",
    ),
    "设计与创作": (
        "3d",
        "audio",
        "behance",
        "canva",
        "design",
        "dribbble",
        "figma",
        "font",
        "image",
        "music",
        "photo",
        "photography",
        "video",
        "创作",
        "字体",
        "图片",
        "摄影",
        "素材",
        "视频",
        "设计",
        "音乐",
    ),
    "效率工具": (
        "convert",
        "download",
        "pdf",
        "productivity",
        "tool",
        "tools",
        "todo",
        "下载",
        "效率",
        "工具",
        "待办",
        "转换",
    ),
    "办公与协作": (
        "calendar",
        "collaboration",
        "email",
        "mail",
        "meeting",
        "notion",
        "office",
        "spreadsheet",
        "workspace",
        "会议",
        "办公",
        "协作",
        "日历",
        "电子表格",
        "邮件",
    ),
    "网络与运维": (
        "cloud",
        "devops",
        "dns",
        "docker",
        "domain",
        "hosting",
        "kubernetes",
        "linux",
        "network",
        "server",
        "vpn",
        "云服务",
        "域名",
        "容器",
        "服务器",
        "网络",
        "运维",
    ),
    "安全与隐私": (
        "antivirus",
        "cybersecurity",
        "firewall",
        "password",
        "privacy",
        "security",
        "安全",
        "密码",
        "防火墙",
        "隐私",
    ),
    "新闻与资讯": (
        "headlines",
        "journal",
        "news",
        "newsletter",
        "rss",
        "日报",
        "新闻",
        "资讯",
    ),
    "社交与社区": (
        "community",
        "forum",
        "reddit",
        "social",
        "社区",
        "社交",
        "论坛",
        "微博",
    ),
    "商业与金融": (
        "bank",
        "business",
        "crypto",
        "economy",
        "finance",
        "investment",
        "stock",
        "trading",
        "商业",
        "投资",
        "股票",
        "财经",
        "金融",
    ),
    "娱乐与媒体": (
        "anime",
        "bilibili",
        "game",
        "gaming",
        "movie",
        "netflix",
        "spotify",
        "steam",
        "stream",
        "youtube",
        "动漫",
        "娱乐",
        "影视",
        "游戏",
    ),
    "生活与服务": (
        "food",
        "health",
        "hotel",
        "map",
        "maps",
        "recipe",
        "restaurant",
        "shop",
        "shopping",
        "travel",
        "健康",
        "地图",
        "旅行",
        "生活",
        "购物",
        "餐厅",
    ),
}

# A generic noun should help, but it must not beat a specific subject. Thus
# "AI 工具" is AI evidence first and only weak productivity evidence.
_GENERIC_KEYWORDS = frozenset(
    {
        "book",
        "docs",
        "documentation",
        "reference",
        "tool",
        "tools",
        "书籍",
        "工具",
        "文档",
        "资料",
    }
)

# Exact/suffix host knowledge is deliberately small and stable. Shared content
# platforms receive less weight because their path/title often carries the real
# subject (for example, an AI repository hosted on GitHub).
_HOST_CATEGORY_RULES: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "AI 与 Agent",
        8,
        (
            "anthropic.com",
            "chatgpt.com",
            "deepseek.com",
            "huggingface.co",
            "midjourney.com",
            "ollama.com",
            "openai.com",
            "replicate.com",
        ),
    ),
    (
        "开发与技术",
        8,
        (
            "developer.mozilla.org",
            "npmjs.com",
            "pypi.org",
            "react.dev",
            "stackoverflow.com",
            "vercel.com",
            "vuejs.org",
        ),
    ),
    ("开发与技术", 5, ("github.com", "github.io", "gitlab.com")),
    ("学习与文档", 8, ("arxiv.org", "coursera.org", "wikipedia.org")),
    ("设计与创作", 8, ("behance.net", "canva.com", "dribbble.com", "figma.com")),
    (
        "办公与协作",
        8,
        ("asana.com", "linear.app", "notion.so", "slack.com", "trello.com"),
    ),
    (
        "网络与运维",
        8,
        ("cloudflare.com", "digitalocean.com", "docker.com", "linode.com"),
    ),
    ("社交与社区", 8, ("reddit.com", "weibo.com")),
    ("商业与金融", 8, ("finance.yahoo.com", "tradingview.com")),
    (
        "娱乐与媒体",
        8,
        ("bilibili.com", "netflix.com", "spotify.com", "steampowered.com", "youtube.com"),
    ),
)

_SIGNAL_WEIGHTS = {
    "folder": 7,
    "host": 4,
    "path": 3,
    "title": 2,
}
_ASCII_WORD = re.compile(r"^[a-z0-9][a-z0-9 .+_-]*$")


def meaningful_folder_path(folder_path: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        folder for folder in folder_path if folder.strip().casefold() not in _BROWSER_ROOT_FOLDERS
    )


@cache
def _keyword_matcher() -> tuple[
    re.Pattern[str] | None,
    tuple[tuple[str, tuple[str, ...]], ...],
    tuple[tuple[str, tuple[str, ...]], ...],
]:
    """Lazily compile one alternation so each metadata field is scanned once."""

    categories_by_keyword: defaultdict[str, list[str]] = defaultdict(list)
    display_keyword: dict[str, str] = {}
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            normalized = keyword.casefold()
            categories_by_keyword[normalized].append(category)
            display_keyword.setdefault(normalized, keyword)

    expressions: list[str] = []
    ascii_entries: list[tuple[str, tuple[str, ...]]] = []
    literal_entries: list[tuple[str, tuple[str, ...]]] = []
    for normalized, categories in categories_by_keyword.items():
        entry = (display_keyword[normalized], tuple(categories))
        if not _ASCII_WORD.fullmatch(normalized):
            literal_entries.append(entry)
            continue
        index = len(ascii_entries)
        pieces = [re.escape(piece) for piece in normalized.split()]
        expression = r"[\s._/+\-]+".join(pieces)
        expressions.append(rf"(?P<k{index}>{expression})")
        ascii_entries.append(entry)
    pattern = (
        re.compile(r"(?<![a-z0-9])(?:" + "|".join(expressions) + r")(?![a-z0-9])")
        if expressions
        else None
    )
    return pattern, tuple(ascii_entries), tuple(literal_entries)


def _matched_keywords(text: str) -> dict[str, tuple[str, ...]]:
    pattern, ascii_entries, literal_entries = _keyword_matcher()
    matched: defaultdict[str, set[str]] = defaultdict(set)
    for keyword, categories in literal_entries:
        if keyword.casefold() not in text:
            continue
        for category in categories:
            matched[category].add(keyword)
    if pattern is not None:
        for match in pattern.finditer(text):
            assert match.lastgroup is not None
            keyword, categories = ascii_entries[int(match.lastgroup.removeprefix("k"))]
            for category in categories:
                matched[category].add(keyword)
    return {
        category: tuple(keyword for keyword in _CATEGORY_KEYWORDS[category] if keyword in found)
        for category, found in matched.items()
    }


def _add_keyword_signal(
    source: str,
    text: str,
    scores: defaultdict[str, int],
    evidence: defaultdict[str, list[str]],
    sources: defaultdict[str, set[str]],
) -> None:
    base_weight = _SIGNAL_WEIGHTS[source]
    for category, matches in _matched_keywords(text).items():
        strongest = max(
            max(1, base_weight - 3) if keyword.casefold() in _GENERIC_KEYWORDS else base_weight
            for keyword in matches
        )
        # Several agreeing words help without allowing keyword stuffing in a
        # long bookmark title to dominate the user's folder intent.
        scores[category] += strongest + min(len(matches) - 1, 2)
        sources[category].add(source)
        evidence[category].extend(f"{source}:{keyword}" for keyword in matches[:4])


def _add_host_rule(
    host: str,
    scores: defaultdict[str, int],
    evidence: defaultdict[str, list[str]],
    sources: defaultdict[str, set[str]],
) -> None:
    comparable = host.rstrip(".").casefold()
    for category, weight, domains in _HOST_CATEGORY_RULES:
        for domain in domains:
            if comparable == domain or comparable.endswith(f".{domain}"):
                scores[category] += weight
                sources[category].add("host")
                evidence[category].append(f"host:{domain}")
                return


def suggest_category(
    folder_path: tuple[str, ...],
    title: str,
    host: str,
    path: str = "",
) -> CategorySuggestion:
    """Suggest a category from bounded, local metadata without an LLM call."""

    signals = {
        "folder": " ".join(meaningful_folder_path(folder_path)).casefold(),
        "host": host.casefold(),
        "path": path.casefold(),
        "title": title.casefold(),
    }
    scores: defaultdict[str, int] = defaultdict(int)
    evidence: defaultdict[str, list[str]] = defaultdict(list)
    sources: defaultdict[str, set[str]] = defaultdict(set)

    _add_host_rule(signals["host"], scores, evidence, sources)
    for source, text in signals.items():
        _add_keyword_signal(source, text, scores, evidence, sources)

    if not scores:
        return CategorySuggestion("未分类", "none", ())
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    category, score = ranked[0]
    runner_category, runner_score = ranked[1] if len(ranked) > 1 else ("", 0)
    margin = score - runner_score

    if score >= 7 and margin >= 3:
        confidence = "high"
    elif score >= 4 and margin >= 2:
        confidence = "medium"
    else:
        conflict = (f"conflict:{runner_category}",) if runner_category else ()
        return CategorySuggestion(
            "未分类",
            "ambiguous",
            tuple((*evidence[category][:7], *conflict)),
        )
    return CategorySuggestion(category, confidence, tuple(evidence[category][:8]))


def combine_category_suggestions(
    rules: CategorySuggestion,
    history: CategorySuggestion | None,
) -> CategorySuggestion:
    """Combine deterministic evidence with a high-confidence account history.

    History fills missing evidence and reinforces agreement. It never silently
    resolves an existing ambiguity or overrides a different rule result.
    """

    if history is None:
        return rules
    if history.confidence == "ambiguous":
        return history
    if rules.confidence == "none":
        return history
    if rules.category == history.category:
        return CategorySuggestion(
            rules.category,
            "high",
            tuple((*history.evidence, *rules.evidence)[:8]),
        )
    if rules.category == "未分类" and rules.confidence == "ambiguous":
        return rules
    return CategorySuggestion(
        "未分类",
        "ambiguous",
        tuple((*history.evidence[:4], *rules.evidence[:3], f"conflict:{rules.category}")),
    )


__all__ = [
    "CLASSIFICATION_RULESET_VERSION",
    "combine_category_suggestions",
    "meaningful_folder_path",
    "suggest_category",
]
