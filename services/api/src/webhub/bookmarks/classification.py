from __future__ import annotations

from collections import defaultdict

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
CLASSIFICATION_RULESET_VERSION = "bookmark-category-rules.v2"

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "AI 与 Agent": (
        "agent",
        "artificial intelligence",
        "chatgpt",
        "deepseek",
        "embedding",
        "gpt",
        "llama",
        "llm",
        "rag",
        "人工智能",
        "大模型",
        "智能体",
    ),
    "开发与技术": (
        "api",
        "code",
        "coding",
        "dev",
        "github",
        "gitlab",
        "java",
        "javascript",
        "python",
        "react",
        "typescript",
        "unity",
        "vercel",
        "代码",
        "开发",
        "编程",
    ),
    "学习与文档": (
        "course",
        "docs",
        "documentation",
        "learn",
        "paper",
        "tutorial",
        "学习",
        "教程",
        "文档",
        "论文",
        "课程",
    ),
    "设计与创作": (
        "3d",
        "audio",
        "design",
        "figma",
        "image",
        "music",
        "photo",
        "video",
        "创作",
        "图片",
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
        "todo",
        "下载",
        "效率",
        "工具",
        "转换",
    ),
    "网络与运维": (
        "cloud",
        "dns",
        "docker",
        "domain",
        "hosting",
        "linux",
        "network",
        "server",
        "vpn",
        "云服务",
        "域名",
        "服务器",
        "网络",
        "运维",
    ),
    "娱乐与媒体": (
        "anime",
        "bilibili",
        "game",
        "movie",
        "stream",
        "动漫",
        "娱乐",
        "影视",
        "游戏",
    ),
    "生活与服务": (
        "health",
        "map",
        "shopping",
        "travel",
        "健康",
        "地图",
        "旅行",
        "生活",
        "购物",
    ),
}


def meaningful_folder_path(folder_path: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        folder for folder in folder_path if folder.strip().casefold() not in _BROWSER_ROOT_FOLDERS
    )


def suggest_category(
    folder_path: tuple[str, ...],
    title: str,
    host: str,
) -> CategorySuggestion:
    folder_text = " ".join(meaningful_folder_path(folder_path)).casefold()
    title_text = title.casefold()
    host_text = host.casefold()
    scores: defaultdict[str, int] = defaultdict(int)
    evidence: defaultdict[str, list[str]] = defaultdict(list)

    for category, keywords in _CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            normalized_keyword = keyword.casefold()
            if normalized_keyword in folder_text:
                scores[category] += 4
                evidence[category].append(f"folder:{keyword}")
            if normalized_keyword in host_text:
                scores[category] += 2
                evidence[category].append(f"host:{keyword}")
            if normalized_keyword in title_text:
                scores[category] += 1
                evidence[category].append(f"title:{keyword}")

    if not scores:
        return CategorySuggestion("未分类", "none", ())
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    category, score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if score >= 4 and score - runner_up >= 2:
        confidence = "high"
    elif score >= 2 and score > runner_up:
        confidence = "medium"
    else:
        return CategorySuggestion("未分类", "ambiguous", tuple(evidence[category][:8]))
    return CategorySuggestion(category, confidence, tuple(evidence[category][:8]))
