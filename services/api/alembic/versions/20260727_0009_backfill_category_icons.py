"""Backfill inferred icons for categories created before icon support.

Revision ID: 20260727_0009
Revises: 52c3f6173b38
Create Date: 2026-07-27
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260727_0009"
down_revision: str | None = "52c3f6173b38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep this migration self-contained. Historical migrations must not import application code whose
# behavior can change after the migration has shipped. Ordering intentionally matches the original
# mapping because equal-length keyword matches use insertion order as their tie-breaker.
_SEMANTIC_ICONS = {
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


def _infer_category_icon(category_name: str) -> str:
    if not category_name:
        return "Folder"

    name_lower = category_name.lower()
    for key in sorted(_SEMANTIC_ICONS, key=len, reverse=True):
        if key not in name_lower:
            continue
        if key in {"ai", "ui", "ux"} and not (
            re.search(r"\b" + key + r"\b", name_lower)
            or name_lower.startswith(key)
            or name_lower.endswith(key)
        ):
            continue
        return _SEMANTIC_ICONS[key]
    return "Folder"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, name FROM categories WHERE icon = 'Folder' ORDER BY id")
    ).mappings()
    updates = [
        {"category_id": row["id"], "icon": inferred}
        for row in rows
        if (inferred := _infer_category_icon(row["name"])) != "Folder"
    ]
    if updates:
        connection.execute(
            sa.text(
                "UPDATE categories SET icon = :icon "
                "WHERE id = :category_id AND icon = 'Folder'"
            ),
            updates,
        )


def downgrade() -> None:
    # Irreversible data repair: an inferred icon cannot be distinguished from the same explicit
    # choice after upgrade. Reverting it would also overwrite valid user choices.
    pass
