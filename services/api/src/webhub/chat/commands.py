from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class SlashCommandDefinition:
    """Metadata only; execution remains in the authenticated Agent pipeline."""

    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    usage: str = ""
    argument_hint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _normalize_command_name(self.name))
        object.__setattr__(
            self,
            "aliases",
            tuple(_normalize_command_name(alias) for alias in self.aliases),
        )


@dataclass(frozen=True, slots=True)
class SlashCommandInvocation:
    is_command: bool
    name: str | None
    argument_text: str
    arguments: tuple[str, ...]
    known: bool
    definition: SlashCommandDefinition | None = None

    def metadata(self) -> dict[str, object]:
        """Return a JSON-safe event suitable for a message metadata sidecar."""

        return {
            "name": self.name,
            "argumentText": self.argument_text,
            "arguments": list(self.arguments),
            "known": self.known,
        }


def _normalize_command_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        raise ValueError("命令名称不能为空")
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if len(normalized) > 64 or any(character.isspace() for character in normalized):
        raise ValueError("命令名称无效")
    return normalized


class SlashCommandRegistry:
    """Small deterministic registry that leaves room for future commands."""

    def __init__(self, definitions: tuple[SlashCommandDefinition, ...] = ()) -> None:
        self._definitions: dict[str, SlashCommandDefinition] = {}
        self._aliases: dict[str, str] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: SlashCommandDefinition) -> None:
        names = (definition.name, *definition.aliases)
        for name in names:
            if name in self._definitions or name in self._aliases:
                raise ValueError(f"命令已注册：{name}")
        self._definitions[definition.name] = definition
        for alias in definition.aliases:
            self._aliases[alias] = definition.name

    def resolve(self, name: str | None) -> SlashCommandDefinition | None:
        if not name:
            return None
        normalized = _normalize_command_name(name)
        canonical = self._aliases.get(normalized, normalized)
        return self._definitions.get(canonical)

    def list(self) -> tuple[SlashCommandDefinition, ...]:
        return tuple(self._definitions.values())

    def snapshot(self) -> Mapping[str, SlashCommandDefinition]:
        return MappingProxyType(
            {definition.name: definition for definition in self._definitions.values()}
        )


_INVOCATION_PATTERN = re.compile(
    r"^\s*/(?P<name>[^\s/]+)(?:\s+(?P<arguments>.*?))?\s*$",
    flags=re.DOTALL,
)


def parse_slash_command(
    text: str,
    *,
    registry: SlashCommandRegistry | None = None,
) -> SlashCommandInvocation:
    """Parse a leading slash without executing or rewriting the message.

    A lone slash is treated as a command-menu invocation. Unknown commands are
    deliberately represented as metadata and are never sent to an LLM by this
    module.
    """

    if not isinstance(text, str):
        raise TypeError("命令文本必须是字符串")
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return SlashCommandInvocation(False, None, "", (), False, None)
    if stripped == "/":
        return SlashCommandInvocation(True, None, "", (), False, None)

    match = _INVOCATION_PATTERN.match(text)
    if match is None:
        return SlashCommandInvocation(True, None, "", (), False, None)
    name = _normalize_command_name(match.group("name"))
    argument_text = (match.group("arguments") or "").strip()
    arguments = tuple(argument_text.split()) if argument_text else ()
    definition = registry.resolve(name) if registry is not None else None
    return SlashCommandInvocation(
        True,
        name,
        argument_text,
        arguments,
        definition is not None,
        definition,
    )


def default_slash_command_registry() -> SlashCommandRegistry:
    return SlashCommandRegistry(
        (
            SlashCommandDefinition(
                name="/搜索",
                aliases=("/search",),
                description="在当前账号的网址库中搜索网站",
                usage="/搜索 <关键词>",
                argument_hint="关键词",
            ),
            SlashCommandDefinition(
                name="/存入",
                aliases=("/save",),
                description="提交一个或多个网址进入收录预览",
                usage="/存入 <URL ...>",
                argument_hint="一个或多个 HTTP(S) URL",
            ),
        )
    )
