"use client";

import { useChat } from "@ai-sdk/react";
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
  Square,
} from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import type { FormEvent, KeyboardEvent } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ConversationThread, type AgentDraftState } from "@/components/agent/conversation-thread";
import { BlurText } from "@/components/react-bits/blur-text";
import {
  confirmAgentSiteDraft,
  listAgentConversations,
  loadAgentConversation,
} from "@/lib/agent-client";
import {
  normalizeAgentMessageMetadata,
  normalizeAgentStreamError,
  normalizeAgentToolCall,
  normalizeAgentToolResult,
  toAgentUIMessages,
  type AgentConversationGroup,
  type AgentSiteDraft,
  type AgentStreamError,
  type AgentToolCall,
  type AgentUIMessage,
} from "@/lib/agent-contract";
import { createAgentChatTransport } from "@/lib/agent-transport";
import { suggestSlashCommands, type SlashCommand } from "@/lib/slash-commands";

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
  const params = useParams<{ conversationId?: string | string[] }>();
  const routeConversationId = useMemo(() => {
    const raw = params?.conversationId;
    const candidate = Array.isArray(raw) ? raw[0] : raw;
    return typeof candidate === "string" && candidate.trim() ? candidate.trim() : null;
  }, [params]);

  const [historyOpen, setHistoryOpen] = useState(false);
  const [searchScope, setSearchScope] = useState<"online" | "collection">("online");
  const [input, setInput] = useState("");
  const [commandIndex, setCommandIndex] = useState(0);
  const [conversationId, setConversationId] = useState<string | null>(routeConversationId);
  const [conversationTitle, setConversationTitle] = useState<string | null>(null);
  const [historyGroups, setHistoryGroups] = useState<readonly AgentConversationGroup[]>([]);
  const [activeToolCalls, setActiveToolCalls] = useState<readonly AgentToolCall[]>([]);
  const [streamError, setStreamError] = useState<AgentStreamError | null>(null);
  const [draftStates, setDraftStates] = useState<Record<string, AgentDraftState>>({});
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Both are read from inside the transport closure, which is built once.
  const conversationIdRef = useRef<string | null>(routeConversationId);
  const searchScopeRef = useRef(searchScope);
  useEffect(() => {
    searchScopeRef.current = searchScope;
  }, [searchScope]);

  const transport = useMemo(
    () =>
      createAgentChatTransport({
        resolveConversationId: () => conversationIdRef.current,
        resolveMetadata: () => ({ webSearch: searchScopeRef.current === "online" }),
      }),
    [],
  );

  const refreshHistory = useCallback(async () => {
    try {
      const history = await listAgentConversations();
      setHistoryGroups(history.groups);
    } catch {
      // History is a convenience panel; a failure here must never block the
      // composer, so the previous snapshot simply stays on screen.
    }
  }, []);

  const { messages, sendMessage, setMessages, status, stop, error } = useChat<AgentUIMessage>({
    transport,
    onData: (part) => {
      if (part.type === "data-agent-tool-call") {
        const call = normalizeAgentToolCall(part.data);
        if (call) setActiveToolCalls((current) => [...current, call]);
        return;
      }
      if (part.type === "data-agent-tool-result") {
        const result = normalizeAgentToolResult(part.data);
        if (result) {
          setActiveToolCalls((current) =>
            current.filter((call) => call.toolCallId !== result.toolCallId),
          );
        }
        return;
      }
      if (part.type === "data-agent-error") {
        const failure = normalizeAgentStreamError(part.data);
        if (failure) setStreamError(failure);
      }
    },
    onFinish: () => {
      setActiveToolCalls([]);
      void refreshHistory();
    },
  });

  useEffect(() => {
    void refreshHistory();
  }, [refreshHistory]);

  // Restore an archived conversation when the route names one.
  useEffect(() => {
    if (routeConversationId === null) return;
    const controller = new AbortController();
    let cancelled = false;
    loadAgentConversation(routeConversationId, controller.signal)
      .then((detail) => {
        if (cancelled) return;
        conversationIdRef.current = detail.conversation.id;
        setConversationId(detail.conversation.id);
        setConversationTitle(detail.conversation.title);
        setMessages(toAgentUIMessages(detail.messages));
      })
      .catch((failure: unknown) => {
        if (cancelled || (failure as Error)?.name === "AbortError") return;
        setStreamError({
          code: "conversation_unavailable",
          message: failure instanceof Error ? failure.message : "会话读取失败，请稍后重试。",
        });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [routeConversationId, setMessages]);

  // The backend mints the conversation id and announces it in the stream, so
  // the client learns it here rather than creating one up front.
  useEffect(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const metadata = normalizeAgentMessageMetadata(messages[index]?.metadata);
      if (!metadata.conversationId) continue;
      if (conversationIdRef.current !== metadata.conversationId) {
        conversationIdRef.current = metadata.conversationId;
        setConversationId(metadata.conversationId);
      }
      return;
    }
  }, [messages]);

  // Keep the address bar shareable without a route change: a real navigation
  // would remount this component and drop the in-flight stream.
  useEffect(() => {
    if (!conversationId || conversationId === routeConversationId) return;
    window.history.replaceState(null, "", `/chat/${encodeURIComponent(conversationId)}`);
  }, [conversationId, routeConversationId]);

  const normalizedInput = input.trimStart();
  const commandPanelOpen = normalizedInput.startsWith("/") && !normalizedInput.includes(" ");
  const filteredCommands = useMemo(() => suggestSlashCommands(input), [input]);
  const busy = status === "submitted" || status === "streaming";
  const conversationStarted = messages.length > 0;

  const chooseCommand = (command: SlashCommand) => {
    setInput(`${command.name} `);
    setCommandIndex(0);
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const submit = useCallback(() => {
    const text = input.trim();
    if (!text || busy) return;
    setStreamError(null);
    setInput("");
    setCommandIndex(0);
    void sendMessage({ text });
  }, [busy, input, sendMessage]);

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
        return;
      }
    }
    // `isComposing` keeps an in-progress Pinyin/IME candidate from sending.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    submit();
  };

  const handleConfirmDraft = useCallback(async (toolCallId: string, draft: AgentSiteDraft) => {
    setDraftStates((current) => ({ ...current, [toolCallId]: { status: "saving" } }));
    try {
      await confirmAgentSiteDraft(draft);
      setDraftStates((current) => ({ ...current, [toolCallId]: { status: "saved" } }));
    } catch (failure: unknown) {
      setDraftStates((current) => ({
        ...current,
        [toolCallId]: {
          status: "error",
          message: failure instanceof Error ? failure.message : "保存失败，请稍后重试。",
        },
      }));
    }
  }, []);

  const errorText = streamError?.message ?? error?.message ?? null;
  const recentItems = historyGroups[0]?.items ?? [];
  // The backend derives a title from the first user message, so the header
  // catches up as soon as the history refresh lands.
  const activeConversation = useMemo(
    () =>
      conversationId === null
        ? null
        : (historyGroups.flatMap((group) => group.items).find((item) => item.id === conversationId) ??
          null),
    [conversationId, historyGroups],
  );
  const headerTitle =
    activeConversation?.title ?? conversationTitle ?? (conversationStarted ? "当前对话" : "新对话");

  return (
    <main className="site-main">
      <header className="agent-page-header">
        <div>
          <span className="page-kicker">Agent</span>
          <h1>{headerTitle}</h1>
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
            ) : (
              historyGroups.map((group) => (
                <div className="history-group" key={group.key}>
                  <div className="history-group-label">{group.label}</div>
                  {group.items.map((item) => (
                    <Link
                      className="history-item"
                      data-active={item.id === conversationId || undefined}
                      href={`/chat/${encodeURIComponent(item.id)}`}
                      key={item.id}
                    >
                      <MessageSquare aria-hidden="true" />
                      <span>{item.title}</span>
                    </Link>
                  ))}
                </div>
              ))
            )}
          </div>
        </section>
      )}

      <section className="agent-workspace" data-started={conversationStarted || undefined}>
        {conversationStarted ? (
          <ConversationThread
            messages={messages}
            status={status}
            activeToolCalls={activeToolCalls}
            draftStates={draftStates}
            onConfirmDraft={handleConfirmDraft}
            errorText={errorText}
            errorCode={streamError?.code ?? null}
          />
        ) : (
          <section className="empty-state" aria-labelledby="empty-state-title">
            <div className="agent-symbol" aria-hidden="true">
              <Sparkles />
            </div>
            <h2 id="empty-state-title">
              <BlurText text="今天想找什么？" />
            </h2>
            <p>收藏的线索、零散的网址，都可以从这里开始。</p>
          </section>
        )}

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
                        <span className="command-icon">
                          <Icon aria-hidden="true" />
                        </span>
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

            <label className="sr-only" htmlFor="agent-input">
              向 Agent 提问
            </label>
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
                <span className="command-hint">
                  <kbd>/</kbd> 命令
                </span>
              </div>
              {busy ? (
                <button
                  className="send-button"
                  type="button"
                  aria-label="停止生成"
                  title="停止生成"
                  onClick={() => void stop()}
                >
                  <Square aria-hidden="true" />
                </button>
              ) : (
                <button
                  className="send-button"
                  type="submit"
                  disabled={!input.trim()}
                  aria-label="发送"
                  title="发送"
                >
                  <ArrowUp aria-hidden="true" />
                </button>
              )}
            </div>
          </form>
          <p className="composer-note">Agent 的新增与修改建议会在确认后执行。</p>
        </div>

        {!conversationStarted && (
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
        )}
      </section>

      {!conversationStarted && (
        <section className="recent-section" aria-labelledby="recent-title">
          <div className="section-heading section-heading-row">
            <h2 id="recent-title">最近对话</h2>
            <button type="button" onClick={() => setHistoryOpen(true)}>
              查看历史记录
            </button>
          </div>
          <div className="recent-list">
            {recentItems.map((item) => (
              <Link
                href={`/chat/${encodeURIComponent(item.id)}`}
                className="recent-item"
                key={item.id}
              >
                <span className="recent-item-icon">
                  <MessageSquare aria-hidden="true" />
                </span>
                <span>{item.title}</span>
                <time dateTime={item.lastMessageAt}>{item.messageCount} 条</time>
              </Link>
            ))}
            {recentItems.length === 0 && <p className="recent-empty">暂无最近对话</p>}
          </div>
        </section>
      )}
    </main>
  );
}
