"use client";

import { useChat } from "@ai-sdk/react";
import {
  ArrowUp,
  ArrowUpRight,
  CircleAlert,
  Globe,
  History,
  Plus,
  Sparkles,
  Square,
} from "lucide-react";
import type { ChangeEvent, FormEvent, KeyboardEvent } from "react";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ConversationThread, type AgentDraftState } from "@/components/agent/conversation-thread";
import { BlurText } from "@/components/react-bits/blur-text";
import { CountUp } from "@/components/react-bits/count-up";
import { PopCheck } from "@/components/react-bits/pop-check";
import { Spinner } from "@/components/react-bits/spinner";
import { StaggerList } from "@/components/react-bits/stagger-list";
import { SiteFavicon } from "@/components/site-favicon";
import {
  confirmAgentSiteDraft,
  confirmAgentSiteUpdate,
  confirmAgentSpaceMembership,
  listAgentConversations,
  loadAgentConversation,
} from "@/lib/agent-client";
import {
  AGENT_SOURCE_LIBRARY,
  AGENT_SOURCE_WEB,
  describeAgentToolResult,
  normalizeAgentMessageMetadata,
  normalizeAgentStreamError,
  normalizeAgentToolCall,
  normalizeAgentToolResult,
  toAgentUIMessages,
  type AgentConversationGroup,
  type AgentDraftAction,
  type AgentStreamError,
  type AgentToolCall,
  type AgentToolLink,
  type AgentUIMessage,
} from "@/lib/agent-contract";
import { createAgentChatTransport } from "@/lib/agent-transport";
import { suggestSlashCommands, type SlashCommand } from "@/lib/slash-commands";

/* ---------- 常量与工具 ---------- */

// 设计稿 1a 的三条快捷提问，点击只填充输入框，发送权始终在用户手里
const QUICK_PROMPTS = [
  "帮我找 Unity API 文档",
  "/存入 ai-bot.cn",
  "把 Figma 移到「设计」并置顶",
] as const;

// 输入框自动增高的上限，对应设计稿"最多约 180px"
const MAX_TEXTAREA_HEIGHT = 180;

type SearchScope = "collection" | "online";

// 阶段进度条的每一步都由真实工具事件驱动：call 事件入场，result 事件定稿
type AgentStage = {
  id: string;
  tool: "search_library" | "web_search";
  status: "active" | "done";
  count: number | null;
  provider: string | null;
};

type WebSaveState = {
  status: "idle" | "saving" | "saved" | "error";
  message?: string;
};

const IDLE_SAVE: WebSaveState = { status: "idle" };

type ResultGroups = {
  library: { items: AgentToolLink[]; total: number };
  web: { items: AgentToolLink[]; provider: string | null };
};

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function cardKey(link: AgentToolLink, index: number): string {
  return link.siteId ?? link.url ?? `${link.name}-${index}`;
}

// 后端 web_search 结果目前不带 provider 字段；这里防御性读取，
// 一旦后端补上就自动显示真实名称，绝不硬编码某家搜索商
function readWebProvider(result: unknown): string | null {
  if (typeof result !== "object" || result === null || Array.isArray(result)) return null;
  const provider = (result as Record<string, unknown>).provider;
  return typeof provider === "string" && provider.trim() ? provider.trim() : null;
}

// 会话标题旁的时间：同天说"今天"，跨天给日期，避免展示裸 ISO 字符串
function formatConversationTime(iso: string): string | null {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const time = date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const now = new Date();
  const dayStart = (value: Date) =>
    new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  const diffDays = Math.round((dayStart(now) - dayStart(date)) / 86_400_000);
  if (diffDays === 0) return `今天 ${time}`;
  if (diffDays === 1) return `昨天 ${time}`;
  if (date.getFullYear() === now.getFullYear()) {
    return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
  }
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
}

/* ---------- 结果卡片（设计稿 1b 的两类卡） ---------- */

function LibraryResultCard({ link }: Readonly<{ link: AgentToolLink }>) {
  return (
    <article className="agent-site-card">
      {/* search_library 的工具结果不含 favicon 地址，交给 SiteFavicon 回落成字母块；28×28 对齐设计稿行 217 */}
      <SiteFavicon url={null} name={link.name} size={28} />
      <div className="agent-site-main">
        <div className="agent-site-title">
          <strong>{link.name}</strong>
          {link.url && <span>{hostOf(link.url)}</span>}
        </div>
        {link.description && <p className="agent-site-desc">{link.description}</p>}
        <div className="agent-site-meta">
          <span className="agent-site-origin">
            收藏库{link.category ? ` · ${link.category}` : ""}
          </span>
          {link.pinned && <span>已置顶</span>}
        </div>
      </div>
      {link.url && (
        <a className="agent-open-button" href={link.url} target="_blank" rel="noreferrer noopener">
          打开
          <ArrowUpRight aria-hidden="true" />
        </a>
      )}
    </article>
  );
}

function WebResultCard({
  link,
  providerLabel,
  state,
  onCollect,
}: Readonly<{
  link: AgentToolLink;
  providerLabel: string;
  state: WebSaveState;
  onCollect: (link: AgentToolLink) => void;
}>) {
  const saved = state.status === "saved";
  return (
    <article className="agent-site-card" data-external="true">
      {/* 28×28 对齐设计稿行 231 */}
      <SiteFavicon url={null} name={link.name} size={28} />
      <div className="agent-site-main">
        <div className="agent-site-title">
          <strong>{link.name}</strong>
          {link.url && <span>{hostOf(link.url)}</span>}
        </div>
        {link.description && <p className="agent-site-desc">{link.description}</p>}
        <div className="agent-site-meta">
          <span className="agent-site-origin" data-tone="accent">
            来源 · {providerLabel}
          </span>
          <span>{saved ? "已收录" : "未收录"}</span>
        </div>
        {state.status === "error" && state.message && (
          <p className="agent-site-error" role="alert">
            {state.message}
          </p>
        )}
      </div>
      <div className="agent-site-actions">
        {link.url && (
          <button
            type="button"
            className="agent-collect-button"
            data-done={saved || undefined}
            disabled={saved || state.status === "saving"}
            onClick={() => onCollect(link)}
          >
            {saved ? (
              <>
                <PopCheck done size={13} />
                已收录
              </>
            ) : state.status === "saving" ? (
              <>
                <Spinner size={12} />
                收录中…
              </>
            ) : (
              <>
                <Plus aria-hidden="true" />
                收录
              </>
            )}
          </button>
        )}
        {link.url && (
          <a
            className="agent-open-button"
            href={link.url}
            target="_blank"
            rel="noreferrer noopener"
          >
            打开
            <ArrowUpRight aria-hidden="true" />
          </a>
        )}
      </div>
    </article>
  );
}

/* ---------- 首页内嵌 Agent 模块 ---------- */

export function AgentPanel() {
  const [scope, setScope] = useState<SearchScope>("collection");
  const [input, setInput] = useState("");
  const [commandIndex, setCommandIndex] = useState(0);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversationTitle, setConversationTitle] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyStatus, setHistoryStatus] = useState<"loading" | "ready" | "error">("loading");
  const [historyGroups, setHistoryGroups] = useState<readonly AgentConversationGroup[]>([]);
  const [activeToolCalls, setActiveToolCalls] = useState<readonly AgentToolCall[]>([]);
  const [stages, setStages] = useState<readonly AgentStage[]>([]);
  const [streamError, setStreamError] = useState<AgentStreamError | null>(null);
  const [draftStates, setDraftStates] = useState<Record<string, AgentDraftState>>({});
  // 「+ 收录」按网址记状态：同一个站点在后续轮次再出现时仍显示"已收录"
  const [webSaves, setWebSaves] = useState<Record<string, WebSaveState>>({});
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  // 历史项快速连点时的请求序号：只认最后一次点击的加载结果，防止旧响应覆盖新会话
  const openRequestRef = useRef(0);

  // transport 只构建一次，闭包里读这两个 ref 拿到每次发送时的最新值
  const conversationIdRef = useRef<string | null>(null);
  const scopeRef = useRef(scope);
  useEffect(() => {
    scopeRef.current = scope;
  }, [scope]);

  const transport = useMemo(
    () =>
      createAgentChatTransport({
        resolveConversationId: () => conversationIdRef.current,
        resolveMetadata: () => ({ webSearch: scopeRef.current === "online" }),
      }),
    [],
  );

  const refreshHistory = useCallback(async (signal?: AbortSignal) => {
    try {
      const history = await listAgentConversations({ signal });
      setHistoryGroups(history.groups);
      setHistoryStatus("ready");
    } catch (failure: unknown) {
      if ((failure as Error)?.name === "AbortError") return;
      // 已经拿到过数据就保留旧快照，历史面板的失败不该打断对话主流程
      setHistoryStatus((current) => (current === "ready" ? current : "error"));
    }
  }, []);

  const { messages, sendMessage, setMessages, status, stop, error } = useChat<AgentUIMessage>({
    transport,
    onData: (part) => {
      if (part.type === "data-agent-tool-call") {
        const call = normalizeAgentToolCall(part.data);
        if (!call) return;
        setActiveToolCalls((current) => [...current, call]);
        // 只有检索类工具进阶段条；写入类（propose_site 等）在会话流里有自己的卡片
        if (call.name === "search_library" || call.name === "web_search") {
          const tool = call.name;
          setStages((current) =>
            current.some((stage) => stage.id === call.toolCallId)
              ? current
              : [
                  ...current,
                  { id: call.toolCallId, tool, status: "active", count: null, provider: null },
                ],
          );
        }
        return;
      }
      if (part.type === "data-agent-tool-result") {
        const result = normalizeAgentToolResult(part.data);
        if (!result) return;
        setActiveToolCalls((current) =>
          current.filter((call) => call.toolCallId !== result.toolCallId),
        );
        setStages((current) =>
          current.flatMap((stage) => {
            if (stage.id !== result.toolCallId) return [stage];
            const view = describeAgentToolResult(result.name, result.result);
            // 失败或无法解析的阶段直接撤下，错误细节由会话流的工具卡片呈现
            if (view.kind !== "links") return [];
            const count =
              stage.tool === "search_library"
                ? (view.matchedCount ?? view.items.length)
                : view.items.length;
            return [
              {
                ...stage,
                status: "done" as const,
                count,
                provider: readWebProvider(result.result),
              },
            ];
          }),
        );
        return;
      }
      if (part.type === "data-agent-error") {
        const failure = normalizeAgentStreamError(part.data);
        if (failure) setStreamError(failure);
      }
    },
    onFinish: () => {
      setActiveToolCalls([]);
      setStages([]);
      void refreshHistory();
    },
  });

  // 首次加载历史：既供「历史」下拉使用，也用来把会话 id 解析成后端派生的标题
  useEffect(() => {
    const controller = new AbortController();
    void refreshHistory(controller.signal);
    return () => controller.abort();
  }, [refreshHistory]);

  // 地址栏带 ?c=<id> 时恢复历史会话。刻意不用 useSearchParams：
  // 那会要求页面套 Suspense，而这里只在挂载时读一次即可
  useEffect(() => {
    const candidate = new URLSearchParams(window.location.search).get("c")?.trim();
    if (!candidate) return;
    const controller = new AbortController();
    let cancelled = false;
    loadAgentConversation(candidate, controller.signal)
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
  }, [setMessages]);

  // 会话 id 由后端在流里下发，客户端从消息元数据里补学
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

  // 用 replaceState 保持地址可分享：真正的路由跳转会重挂组件、掐断进行中的流
  useEffect(() => {
    if (!conversationId) return;
    const url = new URL(window.location.href);
    if (url.searchParams.get("c") === conversationId) return;
    url.searchParams.set("c", conversationId);
    window.history.replaceState(null, "", url);
  }, [conversationId]);

  // 历史下拉：点外部或按 Esc 关闭
  useEffect(() => {
    if (!historyOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (
        historyRef.current &&
        event.target instanceof Node &&
        !historyRef.current.contains(event.target)
      ) {
        setHistoryOpen(false);
      }
    };
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setHistoryOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [historyOpen]);

  const normalizedInput = input.trimStart();
  const commandPanelOpen = normalizedInput.startsWith("/") && !normalizedInput.includes(" ");
  const filteredCommands = useMemo(() => suggestSlashCommands(input), [input]);
  const busy = status === "submitted" || status === "streaming";
  const conversationStarted = messages.length > 0;

  // 输入框自动增高：空闲态与追问态共用同一个 ref（同一时刻只挂载一个）
  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }, [input, conversationStarted]);

  const chooseCommand = (command: SlashCommand) => {
    setInput(`${command.name} `);
    setCommandIndex(0);
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const submit = useCallback(() => {
    const text = input.trim();
    if (!text || busy) return;
    setStreamError(null);
    setStages([]);
    setInput("");
    setCommandIndex(0);
    void sendMessage({ text });
  }, [busy, input, sendMessage]);

  const handleInputChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    setInput(event.target.value);
    setCommandIndex(0);
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
        return;
      }
    }
    // isComposing 保护：拼音等输入法候选确认时的 Enter 不能触发发送
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    submit();
  };

  // 收录 / 修改 / Space 变更三类草稿共用一条确认链路：都在这里落到普通的
  // library / spaces 接口上，写入授权始终来自用户会话，而不是 Agent。
  const handleConfirmDraft = useCallback(async (toolCallId: string, action: AgentDraftAction) => {
    setDraftStates((current) => ({ ...current, [toolCallId]: { status: "saving" } }));
    try {
      if (action.kind === "site") {
        await confirmAgentSiteDraft(action.draft);
      } else if (action.kind === "site_update") {
        await confirmAgentSiteUpdate(action.draft);
      } else {
        await confirmAgentSpaceMembership(action.draft);
      }
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

  // 站外结果「+ 收录」：与草稿卡走同一条确认链路，写入始终由用户的会话授权
  const handleCollect = useCallback(async (link: AgentToolLink) => {
    const url = link.url;
    if (!url) return;
    setWebSaves((current) => ({ ...current, [url]: { status: "saving" } }));
    try {
      await confirmAgentSiteDraft({
        url,
        name: link.name,
        description: link.description ?? "",
        category: link.category ?? "",
        tags: link.tags,
      });
      setWebSaves((current) => ({ ...current, [url]: { status: "saved" } }));
    } catch (failure: unknown) {
      setWebSaves((current) => ({
        ...current,
        [url]: {
          status: "error",
          message: failure instanceof Error ? failure.message : "收录失败，请稍后重试。",
        },
      }));
    }
  }, []);

  const toggleHistory = () => {
    const next = !historyOpen;
    setHistoryOpen(next);
    if (next) void refreshHistory();
  };

  // 历史项刻意不用 <Link href="/?c=...">：Agent 已内嵌在首页，同路由下 Next 只更新
  // query、不会重挂组件，而 ?c 只在挂载时读取一次，跳转后画面并不会真正切换会话。
  // 改成按钮直接在面板内加载目标会话并 replaceState 同步地址，行为最可靠，
  // 也不会打断页面其余部分（吸顶观察、分类滚动）的状态
  const openConversation = useCallback(
    (id: string) => {
      setHistoryOpen(false);
      if (id === conversationIdRef.current) return;
      if (busy) void stop();
      // 清掉上一个会话的瞬态（阶段条 / 草稿 / 收录状态），避免串台
      setStreamError(null);
      setActiveToolCalls([]);
      setStages([]);
      setDraftStates({});
      setWebSaves({});
      const request = ++openRequestRef.current;
      loadAgentConversation(id)
        .then((detail) => {
          if (openRequestRef.current !== request) return;
          conversationIdRef.current = detail.conversation.id;
          setConversationId(detail.conversation.id);
          setConversationTitle(detail.conversation.title);
          setMessages(toAgentUIMessages(detail.messages));
          // replaceState 保持地址可分享，同时避免路由跳转带来的整页重挂
          const url = new URL(window.location.href);
          url.searchParams.set("c", detail.conversation.id);
          window.history.replaceState(null, "", url);
        })
        .catch((failure: unknown) => {
          if (openRequestRef.current !== request) return;
          setStreamError({
            code: "conversation_unavailable",
            message: failure instanceof Error ? failure.message : "会话读取失败，请稍后重试。",
          });
        });
    },
    [busy, stop, setMessages],
  );

  const startNewConversation = () => {
    if (busy) void stop();
    conversationIdRef.current = null;
    setConversationId(null);
    setConversationTitle(null);
    setMessages([]);
    setActiveToolCalls([]);
    setStages([]);
    setStreamError(null);
    setDraftStates({});
    setWebSaves({});
    setInput("");
    setCommandIndex(0);
    setHistoryOpen(false);
    const url = new URL(window.location.href);
    url.searchParams.delete("c");
    window.history.replaceState(null, "", url);
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const applyPrompt = (prompt: string) => {
    setInput(prompt);
    setCommandIndex(0);
    textareaRef.current?.focus();
  };

  const errorText = streamError?.message ?? error?.message ?? null;

  // 后端会从第一句话派生标题；历史刷新落地前先用用户原话顶着
  const firstUserText = useMemo(() => {
    for (const message of messages) {
      if (message.role !== "user") continue;
      const text = message.parts
        .map((part) => (part.type === "text" ? part.text : ""))
        .join("")
        .trim();
      if (text) return text;
    }
    return null;
  }, [messages]);

  const activeConversation = useMemo(
    () =>
      conversationId === null
        ? null
        : (historyGroups
            .flatMap((group) => group.items)
            .find((item) => item.id === conversationId) ?? null),
    [conversationId, historyGroups],
  );
  const headerTitle = activeConversation?.title ?? conversationTitle ?? firstUserText ?? "新对话";
  const headerTime = activeConversation
    ? formatConversationTime(activeConversation.lastMessageAt)
    : null;

  // 设计稿 1b 的结果分组：取最近一条带链接结果的回答，按来源拆成"收藏库 / 网络"两组
  const resultGroups = useMemo<ResultGroups | null>(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message?.role !== "assistant") continue;
      const seen = new Set<string>();
      const library: AgentToolLink[] = [];
      let libraryTotal = 0;
      const web: AgentToolLink[] = [];
      let provider: string | null = null;
      for (const part of message.parts) {
        if (part.type !== "data-agent-tool-result") continue;
        const result = normalizeAgentToolResult(part.data);
        if (!result) continue;
        const view = describeAgentToolResult(result.name, result.result);
        if (view.kind !== "links") continue;
        if (view.source === AGENT_SOURCE_WEB) {
          provider = provider ?? readWebProvider(result.result);
          for (const item of view.items) {
            const key = item.url ?? item.name;
            if (seen.has(key)) continue;
            seen.add(key);
            web.push(item);
          }
        } else if (view.source === AGENT_SOURCE_LIBRARY) {
          // matched_count 是命中总数，可能大于返回条数，分组标题以它为准
          libraryTotal += view.matchedCount ?? view.items.length;
          for (const item of view.items) {
            const key = item.siteId ?? item.url ?? item.name;
            if (seen.has(key)) continue;
            seen.add(key);
            library.push(item);
          }
        }
        // 其他来源的链接（极少见）交给会话流的来源徽标兜底
      }
      if (library.length > 0 || web.length > 0) {
        return { library: { items: library, total: libraryTotal }, web: { items: web, provider } };
      }
    }
    return null;
  }, [messages]);

  const savedCount = useMemo(
    () =>
      resultGroups === null
        ? 0
        : resultGroups.web.items.filter(
            (item) => item.url !== null && webSaves[item.url]?.status === "saved",
          ).length,
    [resultGroups, webSaves],
  );

  // 阶段条内容：真实工具事件 + 流式回答开始后的"正在整理结果…"
  const lastMessage = messages[messages.length - 1];
  const answerStreaming =
    busy &&
    lastMessage?.role === "assistant" &&
    lastMessage.parts.some((part) => part.type === "text" && part.text.trim().length > 0);
  const stageItems: { key: string; label: string; done: boolean }[] = stages.map((stage) => ({
    key: stage.id,
    done: stage.status === "done",
    label:
      stage.tool === "search_library"
        ? stage.status === "done"
          ? `已检索收藏库 · ${stage.count ?? 0} 条匹配`
          : "正在检索收藏库…"
        : stage.status === "done"
          ? `已联网搜索${stage.provider ? ` · ${stage.provider}` : ""} · ${stage.count ?? 0} 个来源`
          : "正在联网搜索…",
  }));
  if (answerStreaming) {
    stageItems.push({ key: "finalize", label: "正在整理结果…", done: false });
  } else if (busy && stageItems.length === 0) {
    stageItems.push({ key: "thinking", label: "正在思考…", done: false });
  }

  const commandPanel = commandPanelOpen ? (
    <div className="agent-command-panel" role="listbox" aria-label="Slash 命令">
      {filteredCommands.length > 0 ? (
        filteredCommands.map((command, index) => (
          <button
            type="button"
            role="option"
            aria-selected={index === commandIndex}
            data-selected={index === commandIndex || undefined}
            key={command.name}
            onMouseEnter={() => setCommandIndex(index)}
            onClick={() => chooseCommand(command)}
          >
            <strong>{command.name}</strong>
            <span>{command.description}</span>
            <code>{command.argument}</code>
          </button>
        ))
      ) : (
        <p className="agent-command-empty">没有匹配的命令</p>
      )}
    </div>
  ) : null;

  return (
    <section
      /* 跨任务契约：顶栏通过观察 #agent-panel 的位置切换吸顶态，id 不能改名 */
      id="agent-panel"
      className="agent-panel"
      data-state={conversationStarted ? "active" : "idle"}
      /* 回答文本流式输出期间置位，CSS 靠它在正文末尾追加闪烁的打字光标 */
      data-streaming={answerStreaming || undefined}
      aria-label="Agent 助手"
    >
      <header className="agent-panel-head">
        <Sparkles className="agent-panel-spark" aria-hidden="true" />
        {conversationStarted ? (
          <>
            <h2 className="agent-panel-title">{headerTitle}</h2>
            {headerTime && <span className="agent-panel-time">{headerTime}</span>}
          </>
        ) : (
          <h2 className="agent-panel-title">
            <BlurText text="今天想找什么网站？" />
          </h2>
        )}
        <div className="agent-head-actions">
          <div className="agent-scope" role="group" aria-label="检索范围">
            <button
              type="button"
              data-active={scope === "collection" || undefined}
              aria-pressed={scope === "collection"}
              onClick={() => setScope("collection")}
            >
              仅收藏库
            </button>
            <button
              type="button"
              data-active={scope === "online" || undefined}
              aria-pressed={scope === "online"}
              onClick={() => setScope("online")}
            >
              <Globe aria-hidden="true" />
              允许联网
            </button>
          </div>
          <div className="agent-history" ref={historyRef}>
            <button
              type="button"
              className="agent-head-button"
              aria-haspopup="menu"
              aria-expanded={historyOpen}
              onClick={toggleHistory}
            >
              <History aria-hidden="true" />
              历史
            </button>
            {historyOpen && (
              <div className="agent-history-menu" role="menu" aria-label="历史会话">
                {historyStatus === "loading" && (
                  <p className="agent-history-note">正在读取历史会话…</p>
                )}
                {historyStatus === "error" && (
                  <p className="agent-history-note" data-tone="danger">
                    历史会话读取失败，请稍后重试。
                  </p>
                )}
                {historyStatus === "ready" && historyGroups.length === 0 && (
                  <p className="agent-history-note">还没有历史会话，完成一次对话后会出现在这里。</p>
                )}
                {historyStatus === "ready" &&
                  historyGroups.map((group) => (
                    <div className="agent-history-group" key={group.key}>
                      <span className="agent-history-label">{group.label}</span>
                      {group.items.map((item) => (
                        <button
                          type="button"
                          role="menuitem"
                          className="agent-history-item"
                          data-active={item.id === conversationId || undefined}
                          key={item.id}
                          onClick={() => openConversation(item.id)}
                        >
                          <History aria-hidden="true" />
                          <span className="agent-history-title">{item.title}</span>
                          <span className="agent-history-count">{item.messageCount} 条</span>
                        </button>
                      ))}
                    </div>
                  ))}
              </div>
            )}
          </div>
          <button type="button" className="agent-head-button" onClick={startNewConversation}>
            <Plus aria-hidden="true" />
            新对话
          </button>
        </div>
      </header>

      {conversationStarted ? (
        <>
          <div className="agent-panel-body">
            <ConversationThread
              messages={messages}
              status={status}
              activeToolCalls={activeToolCalls}
              draftStates={draftStates}
              onConfirmDraft={handleConfirmDraft}
              errorText={errorText}
              errorCode={streamError?.code ?? null}
            />

            {busy && stageItems.length > 0 && (
              <div className="agent-stages" role="status" aria-live="polite">
                {stageItems.map((item, index) => (
                  <Fragment key={item.key}>
                    {index > 0 && <span className="agent-stage-gap" aria-hidden="true" />}
                    <span className="agent-stage" data-done={item.done || undefined}>
                      <PopCheck done={item.done} size={14} />
                      {item.label}
                    </span>
                  </Fragment>
                ))}
              </div>
            )}

            {resultGroups && (
              <div className="agent-results">
                {resultGroups.library.items.length > 0 && (
                  <section className="agent-result-group" aria-label="来自收藏库的结果">
                    <h3 className="agent-result-label">
                      <span>来自收藏库 · {resultGroups.library.total}</span>
                    </h3>
                    <StaggerList className="agent-result-grid">
                      {resultGroups.library.items.map((link, index) => (
                        <LibraryResultCard key={cardKey(link, index)} link={link} />
                      ))}
                    </StaggerList>
                  </section>
                )}
                {resultGroups.web.items.length > 0 && (
                  <section className="agent-result-group" aria-label="来自网络的结果">
                    <h3 className="agent-result-label">
                      <span>
                        来自网络
                        {resultGroups.web.provider
                          ? ` · ${resultGroups.web.provider.toUpperCase()}`
                          : ""}{" "}
                        · {resultGroups.web.items.length}
                      </span>
                      {savedCount > 0 && (
                        <span className="agent-saved-count">
                          已收录 <CountUp value={savedCount} />
                        </span>
                      )}
                    </h3>
                    <StaggerList className="agent-result-grid">
                      {resultGroups.web.items.map((link, index) => (
                        <WebResultCard
                          key={cardKey(link, index)}
                          link={link}
                          providerLabel={resultGroups.web.provider ?? "联网搜索"}
                          state={link.url ? (webSaves[link.url] ?? IDLE_SAVE) : IDLE_SAVE}
                          onCollect={handleCollect}
                        />
                      ))}
                    </StaggerList>
                  </section>
                )}
              </div>
            )}
          </div>

          <footer className="agent-panel-foot">
            <form className="agent-followup" onSubmit={handleSubmit}>
              {commandPanel}
              <label className="sr-only" htmlFor="agent-followup-input">
                继续追问
              </label>
              <textarea
                id="agent-followup-input"
                ref={textareaRef}
                rows={1}
                value={input}
                placeholder="继续追问，或让我把结果加入某个 Space…"
                onChange={handleInputChange}
                onKeyDown={handleInputKeyDown}
              />
              {busy ? (
                <button
                  type="button"
                  className="agent-stop-button"
                  onClick={() => void stop()}
                >
                  <Square aria-hidden="true" />
                  停止生成
                </button>
              ) : (
                <button
                  type="submit"
                  className="agent-send-button"
                  disabled={!input.trim()}
                  aria-label="发送"
                  title="发送"
                >
                  <ArrowUp aria-hidden="true" />
                </button>
              )}
            </form>
          </footer>
        </>
      ) : (
        <>
          <form className="agent-composer" onSubmit={handleSubmit}>
            {commandPanel}
            <label className="sr-only" htmlFor="agent-panel-input">
              描述你要找的网站
            </label>
            <textarea
              id="agent-panel-input"
              ref={textareaRef}
              rows={1}
              value={input}
              placeholder="描述你要找的网站，或粘贴一个/多个 URL 直接入库…"
              onChange={handleInputChange}
              onKeyDown={handleInputKeyDown}
            />
            <div className="agent-composer-bar">
              <span className="agent-command-hint">
                <kbd>/</kbd>
                命令：/搜索 · /存入
              </span>
              <span className="agent-key-hint">Enter 发送 · Shift+Enter 换行</span>
              {busy ? (
                <button
                  type="button"
                  className="agent-send-button"
                  aria-label="停止生成"
                  title="停止生成"
                  onClick={() => void stop()}
                >
                  <Square aria-hidden="true" />
                </button>
              ) : (
                <button
                  type="submit"
                  className="agent-send-button"
                  disabled={!input.trim()}
                  aria-label="发送"
                  title="发送"
                >
                  <ArrowUp aria-hidden="true" />
                </button>
              )}
            </div>
          </form>
          <div className="agent-chips" aria-label="快捷提问">
            {QUICK_PROMPTS.map((prompt) => (
              <button
                type="button"
                className="agent-chip"
                key={prompt}
                onClick={() => applyPrompt(prompt)}
              >
                {prompt}
              </button>
            ))}
          </div>
          {errorText && (
            <p className="agent-panel-error" role="alert">
              <CircleAlert aria-hidden="true" />
              {errorText}
            </p>
          )}
        </>
      )}
    </section>
  );
}
