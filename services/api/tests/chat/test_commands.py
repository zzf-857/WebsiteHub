import pytest

from webhub.chat.commands import (
    SlashCommandDefinition,
    SlashCommandRegistry,
    default_slash_command_registry,
    parse_slash_command,
)


def test_default_commands_are_registered_and_parse_leading_whitespace() -> None:
    registry = default_slash_command_registry()

    invocation = parse_slash_command("  /搜索   Unity API 文档  ", registry=registry)

    assert invocation.is_command is True
    assert invocation.known is True
    assert invocation.definition is not None
    assert invocation.definition.name == "/搜索"
    assert invocation.argument_text == "Unity API 文档"
    assert invocation.arguments == ("Unity", "API", "文档")
    assert parse_slash_command("/search docs", registry=registry).known is True
    assert [definition.name for definition in registry.list()] == ["/搜索", "/存入"]


def test_unknown_and_lone_slash_are_metadata_only() -> None:
    registry = default_slash_command_registry()

    unknown = parse_slash_command("/未来能力 参数", registry=registry)
    menu = parse_slash_command(" / ", registry=registry)
    natural = parse_slash_command("帮我搜索资料", registry=registry)

    assert unknown.metadata() == {
        "name": "/未来能力",
        "argumentText": "参数",
        "arguments": ["参数"],
        "known": False,
    }
    assert menu.is_command is True
    assert menu.name is None
    assert natural.is_command is False


def test_registry_rejects_duplicate_aliases() -> None:
    registry = SlashCommandRegistry((SlashCommandDefinition(name="/one", aliases=("/shared",)),))

    with pytest.raises(ValueError, match="命令已注册"):
        registry.register(SlashCommandDefinition(name="/two", aliases=("/shared",)))
