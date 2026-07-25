"use client";

import {
  ArrowUp,
  Blocks,
  ChevronDown,
  Command,
  Database,
  Globe2,
  Link2,
  MessageSquare,
  Plus,
  Search,
  Sparkles,
} from "lucide-react";
import Link from "next/link";
import type { FormEvent, KeyboardEvent } from "react";
import { useMemo, useRef, useState } from "react";

import { BlurText } from "@/components/react-bits/blur-text";
import {
  suggestSlashCommands,
  type SlashCommand,
} from "@/lib/slash-commands";

const historyGroups: ReadonlyArray<{ label: string; items: ReadonlyArray<string> }> = [];

const commandIcons = {
  search: Search,
  link: Link2,
} as const;

const promptSuggestions = [
  { label: "找出我收藏的 Unity API 文档", icon: Search },
  { label: "整理这些网址到「前端工具」", icon: Sparkles },
  { label: "为下一次旅行建立 Space", icon: Blocks },
] as const;

export function Workbench() {
  const [historyOpen, setHistoryOpen] = useState(false);
  const [searchScope, setSearchScope] = useState<"online" | "collection">("online");
  const [input, setInput] = useState("");
  const [commandIndex, setCommandIndex] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const normalizedInput = input.trimStart();
  const commandPanelOpen = normalizedInput.startsWith("/") && !normalizedInput.includes(" ");
  const filteredCommands = useMemo(() => suggestSlashCommands(input), [input]);

  const chooseCommand = (command: SlashCommand) => {
    setInput(`${command.name} `);
    setCommandIndex(0);
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const handleInputKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (commandPanelOpen && filteredCommands.length > 0) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setCommandIndex((current) => (current + 1) % filteredCommands.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setCommandIndex(
          (current) => (current - 1 + filteredCommands.length) % filteredCommands.length,
        );
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chooseCommand(filteredCommands[commandIndex] ?? filteredCommands[0]);
      }
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
  };

  return (
    <main className="site-main">
        <header className="agent-page-header">
          <div>
            <span className="page-kicker">Agent</span>
            <h1>新对话</h1>
          </div>
          <div className="page-actions">
            <button
              className="history-toggle"
              type="button"
              data-open={historyOpen || undefined}
              aria-expanded={historyOpen}
              aria-controls="conversation-history"
              onClick={() => setHistoryOpen((value) => !value)}
            >
              <MessageSquare aria-hidden="true" />
              <span>历史记录</span>
              <ChevronDown aria-hidden="true" />
            </button>
            <Link className="new-chat-button" href="/chat/new">
              <Plus aria-hidden="true" />
              <span>新建对话</span>
            </Link>
          </div>
        </header>

        {historyOpen && (
          <section className="history-panel" id="conversation-history" aria-labelledby="history-title">
            <div className="history-panel-heading">
              <h2 id="history-title">历史记录</h2>
              <span>按最近活动分组</span>
            </div>
            <div className="history-groups">
              {historyGroups.length === 0 ? (
                <p className="history-empty">还没有历史记录。完成一次对话后会按日期显示在这里。</p>
              ) : historyGroups.map((group) => (
                <div className="history-group" key={group.label}>
                  <div className="history-group-label">{group.label}</div>
                  {group.items.map((item, index) => (
                    <button
                      className="history-item"
                      data-active={group.label === "今天" && index === 0 ? true : undefined}
                      type="button"
                      key={item}
                    >
                      <MessageSquare aria-hidden="true" />
                      <span>{item}</span>
                    </button>
                  ))}
                </div>
              ))}
            </div>
          </section>
        )}

        <section className="agent-workspace" aria-labelledby="empty-state-title">
          <section className="empty-state" aria-labelledby="empty-state-title">
            <div className="agent-symbol" aria-hidden="true">
              <Sparkles />
            </div>
            <h2 id="empty-state-title"><BlurText text="今天想找什么？" /></h2>
            <p>收藏的线索、零散的网址，都可以从这里开始。</p>
          </section>

          <div className="composer-area">
            <div className="composer-context">
              <span>搜索范围</span>
              <div className="search-scope" role="group" aria-label="搜索范围">
                <button
                  type="button"
                  data-active={searchScope === "online" || undefined}
                  aria-pressed={searchScope === "online"}
                  onClick={() => setSearchScope("online")}
                >
                  <Globe2 aria-hidden="true" />
                  <span>允许联网</span>
                </button>
                <button
                  type="button"
                  data-active={searchScope === "collection" || undefined}
                  aria-pressed={searchScope === "collection"}
                  onClick={() => setSearchScope("collection")}
                >
                  <Database aria-hidden="true" />
                  <span>仅收藏库</span>
                </button>
              </div>
            </div>
            <form className="composer" onSubmit={handleSubmit}>
              {commandPanelOpen && (
                <div className="command-panel" role="listbox" aria-label="Slash 命令">
                  <div className="command-panel-label">
                    <Command aria-hidden="true" />
                    <span>命令</span>
                  </div>
                  {filteredCommands.length > 0 ? (
                    filteredCommands.map((command, index) => {
                      const Icon = commandIcons[command.icon];
                      return (
                        <button
                          type="button"
                          role="option"
                          aria-selected={index === commandIndex}
                          data-selected={index === commandIndex || undefined}
                          className="command-item"
                          key={command.name}
                          onMouseEnter={() => setCommandIndex(index)}
                          onClick={() => chooseCommand(command)}
                        >
                          <span className="command-icon"><Icon aria-hidden="true" /></span>
                          <span className="command-copy">
                            <strong>{command.name}</strong>
                            <span>{command.description}</span>
                          </span>
                          <code>{command.argument}</code>
                        </button>
                      );
                    })
                  ) : (
                    <div className="command-empty">没有匹配的命令</div>
                  )}
                </div>
              )}

              <label className="sr-only" htmlFor="agent-input">向 Agent 提问</label>
              <textarea
                id="agent-input"
                ref={textareaRef}
                value={input}
                rows={1}
                placeholder="询问收藏库，或粘贴网址"
                onChange={(event) => {
                  setInput(event.target.value);
                  setCommandIndex(0);
                }}
                onKeyDown={handleInputKeyDown}
              />
              <div className="composer-toolbar">
                <div className="composer-tools">
                  <button className="icon-button" type="button" aria-label="添加网址" title="添加网址">
                    <Plus aria-hidden="true" />
                  </button>
                  <span className="command-hint"><kbd>/</kbd> 命令</span>
                </div>
                <button
                  className="send-button"
                  type="submit"
                  disabled={!input.trim()}
                  aria-label="发送"
                  title="发送"
                >
                  <ArrowUp aria-hidden="true" />
                </button>
              </div>
            </form>
            <p className="composer-note">Agent 的新增与修改建议会在确认后执行。</p>
          </div>

          <section className="prompt-section" aria-labelledby="prompt-section-title">
            <div className="section-heading">
              <h2 id="prompt-section-title">快速开始</h2>
            </div>
            <div className="prompt-list" aria-label="快捷提问">
              {promptSuggestions.map((suggestion) => {
                const Icon = suggestion.icon;
                return (
                  <button
                    type="button"
                    key={suggestion.label}
                    onClick={() => {
                      setInput(suggestion.label);
                      setCommandIndex(0);
                      textareaRef.current?.focus();
                    }}
                  >
                    <Icon aria-hidden="true" />
                    <span>{suggestion.label}</span>
                    <ArrowUp className="prompt-arrow" aria-hidden="true" />
                  </button>
                );
              })}
            </div>
          </section>
        </section>

        <section className="recent-section" aria-labelledby="recent-title">
          <div className="section-heading section-heading-row">
            <h2 id="recent-title">最近对话</h2>
            <button type="button" onClick={() => setHistoryOpen(true)}>查看历史记录</button>
          </div>
          <div className="recent-list">
            {(historyGroups[0]?.items ?? []).map((item, index) => (
              <button type="button" className="recent-item" key={item}>
                <span className="recent-item-icon"><MessageSquare aria-hidden="true" /></span>
                <span>{item}</span>
                <time>{index === 0 ? "刚刚" : "今天"}</time>
              </button>
            ))}
            {historyGroups.length === 0 && (
              <p className="recent-empty">暂无最近对话</p>
            )}
          </div>
        </section>
    </main>
  );
}
