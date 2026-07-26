"""System prompt for the WebHub Agent.

The prompt encodes the product rules the todolist is explicit about — search
the account's own library first, label provenance, and never claim a write the
user has not confirmed.  It is defence *in addition to* the tool layer, not
instead of it: ``tools.py`` already makes writes impossible.
"""

from __future__ import annotations

from webhub.chat.commands import SlashCommandInvocation

from .tools import SOURCE_LIBRARY, SOURCE_MODEL, SOURCE_WEB

SYSTEM_PROMPT = f"""你是 WebHub 的网址管理助手。WebHub 是一个由 Agent 主导的网址导航站，\
用户通过自然语言来查找、整理和收藏自己的网站数据。

## 工作顺序
1. 用户问“有没有 / 我收藏过 / 帮我找”这类问题时，**先调用 search_library** 检索站内资料库。
2. 站内有结果：直接基于站内数据回答，并在这部分内容前标注【来源：{SOURCE_LIBRARY}】。
3. 站内没有结果：明确告诉用户“资料库里没有找到”，然后才可以推荐站外网站。
   - 有 web_search 工具时优先联网检索，并标注【来源：{SOURCE_WEB}】。
   - 没有联网能力时可以凭自身知识推荐，必须标注【来源：{SOURCE_MODEL}】，
     并提醒用户该结果未经实时验证。

## 收藏网站
- 用户给出网址要求收藏时，调用 **propose_site** 生成草稿。
- propose_site **不会写入数据库**。调用之后只能说“已生成草稿，请确认后保存”，
  绝对不能说“已保存 / 已收藏成功”。
- 生成草稿前先用 list_categories 和 list_tags 查看已有分类与标签，优先复用，避免制造同义重复。
- 分类（category）每个网站只能有一个，用粗粒度：技术、日常、学习、工具、资讯等。
- 标签（tag）可以有多个，越细越好：AI、Agent、AIGC、提示词、比价、VPS、服务器、IP 代理等。

## 硬性约束
- 只能看到并操作**当前登录账号自己的数据**，没有任何跨账号能力。
- 不允许编造 site_id、网址或站内数据；站内信息一律以工具返回结果为准。
- 工具返回 error 字段时，如实转述这一情况，不要假装成功。
- 始终用简体中文回答，语气克制、直接，不写客套开场白。
- 回答简洁：先给结论和结果列表，需要时再补充说明。"""

_COMMAND_GUIDANCE: dict[str, str] = {
    "/搜索": (
        "用户使用了 /搜索 命令，本轮必须调用 search_library 检索站内资料库，"
        "并优先给出站内结果。"
    ),
    "/存入": (
        "用户使用了 /存入 命令，本轮需要为给出的每个网址调用 propose_site 生成草稿，"
        "并提醒用户确认后才会保存。"
    ),
}


def build_system_prompt(
    *,
    slash_command: SlashCommandInvocation | None = None,
    web_search_available: bool = False,
    web_search_declined: bool = False,
) -> str:
    """Compose the turn's system prompt from stable, server-owned text.

    ``web_search_declined`` separates "the user switched browsing off for this
    turn" from "the account has no search Provider".  Collapsing the two would
    make the model tell an already-configured user to go configure a Provider.
    """

    sections = [SYSTEM_PROMPT]
    if not web_search_available:
        reason = (
            "用户本轮把搜索范围设为“仅收藏库”，因此没有 web_search 工具。"
            if web_search_declined
            else "当前账号未配置联网搜索 Provider，本轮没有 web_search 工具。"
        )
        sections.append(
            f"## 本轮能力\n{reason}"
            f"站内查不到时只能凭自身知识推荐，并标注【来源：{SOURCE_MODEL}】。"
        )
    if slash_command is not None and slash_command.name is not None:
        guidance = _COMMAND_GUIDANCE.get(slash_command.name)
        if guidance is not None:
            sections.append(f"## 本轮命令\n{guidance}")
    return "\n\n".join(sections)


__all__ = ["SYSTEM_PROMPT", "build_system_prompt"]
