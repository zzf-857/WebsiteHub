"""Semantic mapping for category names to Lucide icons."""

import re

# Mapping of keywords/semantics to Lucide icon names.
# Keys are lowercase for case-insensitive matching.
# We will use simple keyword presence or regex for matching.
SEMANTIC_ICONS = {
    "ai": "Bot",
    "人工智能": "Bot",
    "大模型": "Bot",
    "gpt": "Bot",
    "llm": "Bot",
    "设计": "PenTool",
    "design": "PenTool",
    "ui": "Layout",
    "ux": "Layout",
    "原型": "LayoutTemplate",
    "开发": "Code",
    "dev": "Code",
    "code": "Code",
    "编程": "Code",
    "前端": "Monitor",
    "后端": "Server",
    "数据库": "Database",
    "运维": "Terminal",
    "商业": "Briefcase",
    "business": "Briefcase",
    "办公": "Briefcase",
    "阅读": "BookOpen",
    "read": "BookOpen",
    "文章": "FileText",
    "文档": "FileText",
    "学习": "GraduationCap",
    "learn": "GraduationCap",
    "教程": "Book",
    "工具": "Wrench",
    "tools": "Wrench",
    "效率": "Zap",
    "productivity": "Zap",
    "生活": "Coffee",
    "life": "Coffee",
    "社交": "Users",
    "social": "Users",
    "娱乐": "Gamepad2",
    "game": "Gamepad2",
    "视频": "Video",
    "video": "Video",
    "音乐": "Music",
    "music": "Music",
    "图片": "Image",
    "素材": "Image",
    "摄影": "Camera",
    "新闻": "Newspaper",
    "news": "Newspaper",
    "财经": "LineChart",
    "finance": "LineChart",
    "投资": "LineChart",
    "科技": "Cpu",
    "tech": "Cpu",
    "购物": "ShoppingCart",
    "shop": "ShoppingCart",
    "云": "Cloud",
    "cloud": "Cloud",
    "安全": "Shield",
    "security": "Shield",
    "健康": "Heart",
    "health": "Heart",
    "项目": "Kanban",
    "project": "Kanban",
    "知识库": "Library",
    "wiki": "Library",
    "想法": "Lightbulb",
    "idea": "Lightbulb",
    "灵感": "Lightbulb",
    "收藏": "Star",
    "书签": "Bookmark",
    "个人": "User",
}


def infer_category_icon(category_name: str) -> str:
    """
    Infer a Lucide icon based on the category name's semantics.
    Falls back to 'Folder' if no match is found.
    """
    if not category_name:
        return "Folder"

    name_lower = category_name.lower()

    # Check for exact or substring matches in our dictionary
    sorted_keys = sorted(SEMANTIC_ICONS.keys(), key=len, reverse=True)

    for key in sorted_keys:
        if key in name_lower:
            # Special case for 'ai', 'ui', 'ux' to avoid matching words like 'mail' or 'main'
            if key in ("ai", "ui", "ux"):
                if (
                    re.search(r"\b" + key + r"\b", name_lower)
                    or name_lower.startswith(key)
                    or name_lower.endswith(key)
                ):
                    return SEMANTIC_ICONS[key]
            else:
                return SEMANTIC_ICONS[key]

    return "Folder"
