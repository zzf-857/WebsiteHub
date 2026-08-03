"use client";

// 从 agent-panel.tsx 抽出来的有状态逻辑。
//
// 动机是可读性而不是行数：原组件把几百行 state/effect/handler 和同样量级的 JSX
// 摞在一个函数里，改一处标记要先翻过整套数据加载。分开之后，「数据怎么来」和
// 「长什么样」各看各的一个文件。
//
// 返回 38 个值看着多，但这就是原来那段逻辑本来就要交给 JSX 的东西——
// 拆分只是把这份契约显式写出来，没有新增耦合。

import {
  useChat,
} from "@ai-sdk/react";
import type { ChatInit } from "ai";
import type {
  ChangeEvent,
  FormEvent,
  KeyboardEvent,
} from "react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  type AgentDraftState,
} from "@/components/agent/conversation-thread";
import {
  confirmAgentSiteBatch,
  confirmAgentSiteDraft,
  confirmAgentSiteUpdate,
  confirmAgentSpaceBatch,
  confirmAgentSpaceMembership,
  AgentApiError,
  recordAgentDraftConfirmation,
  listAllAgentConversations,
  loadAgentConversation,
  type AgentDraftConfirmationInput,
} from "@/lib/agent-client";
import { confirmAgentReclassify } from "@/lib/library-client";

import {
  AGENT_SOURCE_LIBRARY,
  AGENT_SOURCE_MODEL,
  AGENT_SOURCE_WEB,
  MAX_AGENT_MESSAGE_LENGTH,
  agentToolLabel,
  confirmedAgentDraftToolCallIds,
  describeAgentToolResult,
  normalizeAgentMessageMetadata,
  normalizeAgentStreamError,
  normalizeAgentToolCall,
  normalizeAgentToolResult,
  toAgentUIMessages,
  type AgentConversationGroup,
  type AgentDraftAction,
  type AgentStreamError,
  type AgentStoredMessage,
  type AgentToolCall,
  type AgentToolLink,
  type AgentUIMessage,
} from "@/lib/agent-contract";
import {
  createAgentChatTransport,
} from "@/lib/agent-transport";
import {
  suggestSlashCommands,
  type SlashCommand,
} from "@/lib/slash-commands";
import {
  AgentStage,
  ResultGroups,
  SearchScope,
  WebSaveState,
  formatConversationTime,
  readWebProvider,
} from "@/components/agent/agent-result-cards";

/* ---------- 常量与工具 ---------- */
// 设计稿 1a 的三条快捷提问，点击只填充输入框，发送权始终在用户手里。
// 这三条在 593d4dc 抽 hook 时被误清空，导致 agent-panel 里的 .agent-chips 容器
// 渲染了却一个 chip 都没有；三条对应的能力（检索 / 存入 / 改分类置顶）都真实可用。
export const QUICK_PROMPTS = [
  "帮我找 Unity API 文档",
  "/存入 https://ai-bot.cn",
  "把 Figma 移到「设计」并置顶",
] as const;
// 输入框自动增高的上限，对应设计稿"最多约 180px"
const MAX_TEXTAREA_HEIGHT = 180;

function restoredDraftStates(
  messages: readonly AgentStoredMessage[],
): Record<string, AgentDraftState> {
  const states: Record<string, AgentDraftState> = {};
  for (const toolCallId of confirmedAgentDraftToolCallIds(messages)) {
    states[toolCallId] = { status: "saved" };
  }
  return states;
}

function completedAgentStage(result: ReturnType<typeof normalizeAgentToolResult>): AgentStage | null {
  if (!result) return null;
  const view = describeAgentToolResult(result.name, result.result);
  const count = view.kind === "links"
    ? (view.matchedCount ?? view.items.length)
    : null;
  return {
    id: result.toolCallId,
    tool: result.name,
    status: "done",
    count,
    provider: readWebProvider(result.result),
  };
}

function restoredAgentStages(messages: readonly AgentStoredMessage[]): AgentStage[] {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== "assistant") continue;
    const stages = new Map<string, AgentStage>();
    for (const source of message.sources) {
      if ("type" in source && source.type === "source-url") continue;
      const stage = completedAgentStage(normalizeAgentToolResult(source));
      if (stage) stages.set(stage.id, stage);
    }
    // The timeline belongs to the latest assistant turn. Falling through to an
    // older answer would make a tool-free or interrupted turn display stale work.
    return [...stages.values()];
  }
  return [];
}

type UseAgentPanelOptions = {
  onLibraryChanged?: () => void;
};

type AgentOnData = NonNullable<ChatInit<AgentUIMessage>["onData"]>;
type AgentOnError = NonNullable<ChatInit<AgentUIMessage>["onError"]>;
type AgentOnFinish = NonNullable<ChatInit<AgentUIMessage>["onFinish"]>;
type PendingDraftConfirmation = {
  conversationId: string;
  confirmation: AgentDraftConfirmationInput;
};

const SPACE_BATCH_WRITE_TIMEOUT_MS = 30_000;
const DRAFT_CONFIRMATION_TIMEOUT_MS = 15_000;

function pendingDraftConfirmationKey(conversationId: string, toolCallId: string): string {
  return JSON.stringify([conversationId, toolCallId]);
}

function confirmationFailureState(failure: unknown): AgentDraftState {
  const retryable = !(failure instanceof AgentApiError) ||
    failure.status === 408 || failure.status === 429 || failure.status >= 500;
  const failureMessage = failure instanceof Error ? failure.message : "未知错误";
  return {
    status: "error",
    confirmationPending: true,
    blocksConversation: retryable,
    message: retryable
      ? "数据已写入，但会话状态同步失败。点击重试只会补同步，不会重复写入。"
      : `数据已写入，但会话状态无法同步：${failureMessage}`,
  };
}

function transportErrorMessage(error: Error | undefined): string | null {
  // Transport/runtime diagnostics are not suitable as interface copy. The
  // backend's structured failures arrive through data-agent-error instead.
  return error ? "本次回答意外中断，请重试。" : null;
}

export function useAgentPanel({ onLibraryChanged }: UseAgentPanelOptions = {}) {
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
  const historyTriggerRef = useRef<HTMLButtonElement>(null);
  const historyInitialFocusRef = useRef<"active" | "first" | "last">("active");
  const commandPanelId = useId();
  // 历史项快速连点时的请求序号：只认最后一次点击的加载结果，防止旧响应覆盖新会话
  const openRequestRef = useRef(0);
  // 业务写入成功、会话 marker 失败时保留第二阶段；重试只补 marker，绝不重放业务写入。
  const pendingDraftConfirmationsRef = useRef(new Map<string, PendingDraftConfirmation>());

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
      const history = await listAllAgentConversations({ signal });
      setHistoryGroups(history.groups);
      setHistoryStatus("ready");
    } catch (failure: unknown) {
      if ((failure as Error)?.name === "AbortError") return;
      // 已经拿到过数据就保留旧快照，历史面板的失败不该打断对话主流程
      setHistoryStatus((current) => (current === "ready" ? current : "error"));
    }
  }, []);

  const handleStreamData = useCallback<AgentOnData>((part) => {
    if (part.type === "data-agent-tool-call") {
      const call = normalizeAgentToolCall(part.data);
      if (!call) return;
      setActiveToolCalls((current) => {
        const index = current.findIndex((item) => item.toolCallId === call.toolCallId);
        if (index < 0) return [...current, call];
        const previous = current[index];
        if (previous?.name === call.name) return current;
        return current.map((item, itemIndex) => (itemIndex === index ? call : item));
      });
      setStages((current) =>
        current.some((stage) => stage.id === call.toolCallId)
          ? current
          : [
              ...current,
              {
                id: call.toolCallId,
                tool: call.name,
                status: "active",
                count: null,
                provider: null,
              },
            ],
      );
      return;
    }
    if (part.type === "data-agent-tool-result") {
      const result = normalizeAgentToolResult(part.data);
      if (!result) return;
      setActiveToolCalls((current) => {
        const next = current.filter((call) => call.toolCallId !== result.toolCallId);
        return next.length === current.length ? current : next;
      });
      setStages((current) => {
        const index = current.findIndex((stage) => stage.id === result.toolCallId);
        const completed = completedAgentStage(result);
        if (!completed) return current;
        if (index < 0) return [...current, completed];
        return current.map((item, stageIndex) => (stageIndex === index ? completed : item));
      });
      return;
    }
    if (part.type === "data-agent-error") {
      const failure = normalizeAgentStreamError(part.data);
      if (!failure) return;
      setStreamError((current) =>
        current?.code === failure.code && current.message === failure.message ? current : failure,
      );
    }
  }, []);

  const settleTransientToolState = useCallback(() => {
    setActiveToolCalls((current) => (current.length === 0 ? current : []));
    setStages((current) => {
      const completed = current.filter((stage) => stage.status === "done");
      return completed.length === current.length ? current : completed;
    });
  }, []);

  const handleStreamError = useCallback<AgentOnError>(() => {
    settleTransientToolState();
  }, [settleTransientToolState]);

  const handleStreamFinish = useCallback<AgentOnFinish>(() => {
    settleTransientToolState();
    void refreshHistory();
  }, [refreshHistory, settleTransientToolState]);

  const {
    messages,
    sendMessage,
    setMessages,
    status,
    stop: stopChat,
    error,
    clearError,
  } = useChat<AgentUIMessage>({
    transport,
    throttle: 50,
    onData: handleStreamData,
    onError: handleStreamError,
    onFinish: handleStreamFinish,
  });

  // A raw transport failure may arrive after the stream's start metadata but
  // before the server can send terminal metadata. Close that local Assistant
  // snapshot explicitly so the stale `streaming` flag cannot survive into a
  // later turn or contradict the visible transport error.
  useEffect(() => {
    if (!error) return;
    setMessages((current) => {
      for (let index = current.length - 1; index >= 0; index -= 1) {
        const message = current[index];
        if (message?.role === "user") break;
        if (message?.role !== "assistant") continue;
        const metadata = normalizeAgentMessageMetadata(message.metadata);
        if (metadata.messageStatus === "error") return current;
        return current.map((item, itemIndex) => itemIndex === index
          ? {
              ...item,
              metadata: {
                ...metadata,
                messageStatus: "error" as const,
              },
            }
          : item);
      }
      return current;
    });
  }, [error, setMessages]);

  const stop = useCallback(async () => {
    await stopChat();
    settleTransientToolState();
    // Aborting the browser stream prevents a terminal metadata chunk from
    // reaching the client. Keep the partial assistant bubble and mark the
    // local snapshot immediately; a history reload will use the server state.
    setMessages((current) => {
      let assistantIndex = -1;
      for (let index = current.length - 1; index >= 0; index -= 1) {
        if (current[index]?.role === "assistant") {
          assistantIndex = index;
          break;
        }
        if (current[index]?.role === "user") break;
      }
      if (assistantIndex < 0) {
        const userMessage = current[current.length - 1];
        if (userMessage?.role !== "user") return current;
        return [
          ...current,
          {
            id: `assistant-aborted-${userMessage.id}`,
            role: "assistant" as const,
            parts: [],
            metadata: {
              ...(conversationIdRef.current
                ? { conversationId: conversationIdRef.current }
                : {}),
              turnId: userMessage.id,
              messageStatus: "aborted" as const,
              turnPersisted: false,
            },
          },
        ];
      }
      return current.map((message, index) => index === assistantIndex
        ? {
            ...message,
            metadata: {
              ...normalizeAgentMessageMetadata(message.metadata),
              messageStatus: "aborted" as const,
            },
          }
        : message);
    });
  }, [setMessages, settleTransientToolState, stopChat]);

  // 首次加载历史：既供「历史」下拉使用，也用来把会话 id 解析成后端派生的标题
  useEffect(() => {
    const controller = new AbortController();
    void refreshHistory(controller.signal);
    return () => controller.abort();
  }, [refreshHistory]);

  // 地址栏带 ?ask=<提问> 时把输入框预填好并聚焦，但**不自动发送**：
  // 从站点详情跳过来的快捷入口是「帮我起个头」，不是「替我下决定」，
  // 用户还得能改措辞。与 ?c= 同样只在挂载时读一次。
  useEffect(() => {
    const asked = new URLSearchParams(window.location.search).get("ask")?.trim();
    if (!asked) return;
    // 按码点截断：契约那边也是 Array.from().length，slice 数的是 UTF-16 单元，
    // 两种数法在 emoji / 补充平面字符上会对不齐
    setInput(Array.from(asked).slice(0, MAX_AGENT_MESSAGE_LENGTH).join(""));
    window.requestAnimationFrame(() => {
      const element = textareaRef.current;
      if (!element) return;
      element.focus();
      // 光标落到末尾，用户可以直接接着补充，而不是覆盖整段
      element.setSelectionRange(element.value.length, element.value.length);
    });
  }, []);

  // 地址栏带 ?c=<id> 时恢复历史会话。刻意不用 useSearchParams：
  // 那会要求页面套 Suspense，而这里只在挂载时读一次即可
  useEffect(() => {
    const candidate = new URLSearchParams(window.location.search).get("c")?.trim();
    if (!candidate) return;
    const controller = new AbortController();
    const request = ++openRequestRef.current;
    let cancelled = false;
    loadAgentConversation(candidate, controller.signal)
      .then((detail) => {
        if (cancelled || openRequestRef.current !== request) return;
        conversationIdRef.current = detail.conversation.id;
        setConversationId(detail.conversation.id);
        setConversationTitle(detail.conversation.title);
        setMessages(toAgentUIMessages(detail.messages));
        setDraftStates(restoredDraftStates(detail.messages));
        setStages(restoredAgentStages(detail.messages));
      })
      .catch((failure: unknown) => {
        if (
          cancelled ||
          openRequestRef.current !== request ||
          (failure as Error)?.name === "AbortError"
        ) return;
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

  // 历史下拉：点外部或按 Esc 关闭；键盘关闭时把焦点还给触发按钮。
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
      if (event.key !== "Escape") return;
      event.preventDefault();
      setHistoryOpen(false);
      window.requestAnimationFrame(() => historyTriggerRef.current?.focus());
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [historyOpen]);

  // 菜单数据可能在打开后才异步到达，因此在 ready 后再把焦点移入菜单。
  useEffect(() => {
    if (!historyOpen || historyStatus !== "ready") return;
    const frame = window.requestAnimationFrame(() => {
      const items = Array.from(
        historyRef.current?.querySelectorAll<HTMLButtonElement>(
          '[role="menuitem"]:not(:disabled)',
        ) ?? [],
      );
      if (items.length === 0) return;
      const preferred = historyInitialFocusRef.current;
      const target = preferred === "last"
        ? items[items.length - 1]
        : preferred === "active"
          ? items.find((item) => item.hasAttribute("data-active")) ?? items[0]
          : items[0];
      historyInitialFocusRef.current = "active";
      target?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [historyGroups, historyOpen, historyStatus]);

  const normalizedInput = input.trimStart();
  const commandPanelOpen = normalizedInput.startsWith("/") && !normalizedInput.includes(" ");
  const filteredCommands = useMemo(() => suggestSlashCommands(input), [input]);
  const busy = status === "submitted" || status === "streaming";
  const draftWorkflowBusy = Object.values(draftStates).some(
    (state) => state.blocksConversation === true,
  );
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
    if (!text || busy || draftWorkflowBusy) return;
    // 用户已经选择继续当前画面，任何尚未完成的历史会话读取都不得再覆盖它。
    openRequestRef.current += 1;
    clearError();
    setStreamError(null);
    setActiveToolCalls([]);
    setStages([]);
    setInput("");
    setCommandIndex(0);
    setHistoryOpen(false);
    void sendMessage({ text });
  }, [busy, clearError, draftWorkflowBusy, input, sendMessage]);

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

  // 所有草稿共用一条确认链路：都先落到普通业务接口，再持久化确认标记；
  // 写入授权始终来自用户会话，而不是 Agent。
  const handleConfirmDraft = useCallback(async (toolCallId: string, action: AgentDraftAction) => {
    const visibleConversationId = conversationIdRef.current;
    if (!visibleConversationId) {
      setDraftStates((current) => ({
        ...current,
        [toolCallId]: {
          status: "error",
          message: "会话状态尚未同步，请稍后再确认。",
        },
      }));
      return;
    }
    const pendingKey = pendingDraftConfirmationKey(visibleConversationId, toolCallId);
    const pendingConfirmation = pendingDraftConfirmationsRef.current.get(pendingKey);
    const originatingConversationId =
      pendingConfirmation?.conversationId ?? visibleConversationId;
    // A history load started just before this click must not switch the visible
    // conversation while the approved write and its marker are in flight.
    openRequestRef.current += 1;
    setHistoryOpen(false);
    const updateOriginDraftState = (next: AgentDraftState) => {
      if (conversationIdRef.current !== originatingConversationId) return;
      setDraftStates((current) => ({ ...current, [toolCallId]: next }));
    };

    updateOriginDraftState({
      status: "saving",
      blocksConversation: pendingConfirmation !== undefined || action.kind === "space_batch",
    });
    try {
      if (pendingConfirmation) {
        try {
          await recordAgentDraftConfirmation(
            pendingConfirmation.conversationId,
            pendingConfirmation.confirmation,
            AbortSignal.timeout(DRAFT_CONFIRMATION_TIMEOUT_MS),
          );
        } catch (failure: unknown) {
          updateOriginDraftState(confirmationFailureState(failure));
          return;
        }
        pendingDraftConfirmationsRef.current.delete(pendingKey);
        updateOriginDraftState({ status: "saved" });
        return;
      }

      let confirmation: Parameters<typeof recordAgentDraftConfirmation>[1] | null = null;
      if (action.kind === "site") {
        const created = await confirmAgentSiteDraft(action.draft);
        onLibraryChanged?.();
        confirmation = { toolCallId, kind: "site_created", siteId: created.id };
      } else if (action.kind === "site_update") {
        const updated = await confirmAgentSiteUpdate(action.draft);
        onLibraryChanged?.();
        confirmation = { toolCallId, kind: "site_updated", siteId: updated.id };
      } else if (action.kind === "site_batch") {
        const result = await confirmAgentSiteBatch(action.draft);
        if (result.created > 0) onLibraryChanged?.();
        if (result.failed > 0) {
          throw new Error(
            `批量收录未完全完成：新增 ${result.created} 个，重复 ${result.duplicate} 个，失败 ${result.failed} 个。请重试失败项。`,
          );
        }
        confirmation = { toolCallId, kind: "site_batch_created" };
      } else if (action.kind === "space_membership") {
        await confirmAgentSpaceMembership(action.draft);
        onLibraryChanged?.();
        confirmation = {
          toolCallId,
          kind: action.draft.action === "add" ? "space_member_added" : "space_member_removed",
          siteId: action.draft.siteId,
          spaceId: action.draft.spaceId,
        };
      } else if (action.kind === "space_batch") {
        const result = await confirmAgentSpaceBatch(
          action.draft,
          action.selectedSiteIds,
          toolCallId,
          AbortSignal.timeout(SPACE_BATCH_WRITE_TIMEOUT_MS),
        );
        onLibraryChanged?.();
        confirmation = {
          toolCallId,
          kind: "space_batch_applied",
          spaceId: result.space.id,
          siteIds: result.siteIds,
        };
      } else if (action.kind === "reclassify") {
        await confirmAgentReclassify(action.draft);
        onLibraryChanged?.();
        confirmation = { toolCallId, kind: "reclassify_applied" };
      }
      if (confirmation === null) {
        throw new Error("当前版本不支持确认这类 Agent 草稿。");
      }

      // 把「已确认」写回会话，否则下一轮回放到的历史仍然说这张草稿没生效，
      // Agent 会否认它自己刚存过的东西。marker 失败时保留第二阶段，按钮重试
      // 只补这条记录，不会再次执行已经成功的业务写入。
      pendingDraftConfirmationsRef.current.set(pendingKey, {
        conversationId: originatingConversationId,
        confirmation,
      });
      try {
        await recordAgentDraftConfirmation(
          originatingConversationId,
          confirmation,
          AbortSignal.timeout(DRAFT_CONFIRMATION_TIMEOUT_MS),
        );
      } catch (failure: unknown) {
        updateOriginDraftState(confirmationFailureState(failure));
        return;
      }
      pendingDraftConfirmationsRef.current.delete(pendingKey);
      updateOriginDraftState({ status: "saved" });
    } catch (failure: unknown) {
      updateOriginDraftState({
        status: "error",
        message: failure instanceof Error ? failure.message : "保存失败，请稍后重试。",
      });
    }
  }, [onLibraryChanged]);

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
      onLibraryChanged?.();
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
  }, [onLibraryChanged]);

  const toggleHistory = () => {
    const next = !historyOpen;
    if (next) historyInitialFocusRef.current = "active";
    setHistoryOpen(next);
    if (next) void refreshHistory();
  };

  const handleHistoryTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    historyInitialFocusRef.current = event.key === "ArrowUp" ? "last" : "first";
    if (!historyOpen) {
      setHistoryOpen(true);
      void refreshHistory();
    }
  };

  const handleHistoryMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setHistoryOpen(false);
      window.requestAnimationFrame(() => historyTriggerRef.current?.focus());
      return;
    }
    if (event.key === "Tab") {
      event.preventDefault();
      event.stopPropagation();
      const trigger = historyTriggerRef.current;
      const menu = event.currentTarget;
      const scope = trigger?.closest(".agent-panel-head");
      const controls = Array.from(
        scope?.querySelectorAll<HTMLElement>(
          'a[href]:not([tabindex="-1"]), button:not([disabled]):not([tabindex="-1"])',
        ) ?? [],
      ).filter((element) => !menu.contains(element));
      const triggerIndex = trigger ? controls.indexOf(trigger) : -1;
      const offset = event.shiftKey ? -1 : 1;
      const nextIndex = triggerIndex >= 0 && controls.length > 0
        ? (triggerIndex + offset + controls.length) % controls.length
        : -1;
      setHistoryOpen(false);
      window.requestAnimationFrame(() => {
        if (nextIndex >= 0) controls[nextIndex]?.focus();
        else trigger?.focus();
      });
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;

    const items = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>(
        '[role="menuitem"]:not(:disabled)',
      ),
    );
    if (items.length === 0) return;
    event.preventDefault();
    const currentIndex = items.findIndex((item) => item === document.activeElement);
    let nextIndex = currentIndex;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = items.length - 1;
    else if (event.key === "ArrowDown") nextIndex = currentIndex < items.length - 1 ? currentIndex + 1 : 0;
    else nextIndex = currentIndex > 0 ? currentIndex - 1 : items.length - 1;
    items[nextIndex]?.focus();
  };

  // 历史项刻意不用 <Link href="/?c=...">：Agent 已内嵌在首页，同路由下 Next 只更新
  // query、不会重挂组件，而 ?c 只在挂载时读取一次，跳转后画面并不会真正切换会话。
  // 改成按钮直接在面板内加载目标会话并 replaceState 同步地址，行为最可靠，
  // 也不会打断页面其余部分（吸顶观察、分类滚动）的状态
  const openConversation = useCallback(
    (id: string) => {
      if (busy || draftWorkflowBusy) return;
      setHistoryOpen(false);
      window.requestAnimationFrame(() => historyTriggerRef.current?.focus());
      if (id === conversationIdRef.current) return;
      // 清掉上一个会话的瞬态（阶段条 / 草稿 / 收录状态），避免串台
      clearError();
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
          setDraftStates(restoredDraftStates(detail.messages));
          setStages(restoredAgentStages(detail.messages));
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
    [busy, clearError, draftWorkflowBusy, setMessages],
  );

  const startNewConversation = () => {
    if (busy || draftWorkflowBusy) return;
    openRequestRef.current += 1;
    clearError();
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

  const errorText = streamError?.message ?? transportErrorMessage(error);

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

  // 结果卡只属于当前一轮回答。新问题发出后不能继续展示上一轮卡片，
  // 否则看起来像 Agent 把旧推荐带进了新答案。
  const resultGroups = useMemo<ResultGroups | null>(() => {
    if (status === "submitted" && messages[messages.length - 1]?.role !== "user") {
      return null;
    }
    let message: AgentUIMessage | undefined;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const candidate = messages[index];
      if (candidate?.role === "assistant") {
        message = candidate;
        break;
      }
      if (candidate?.role === "user") return null;
    }
    if (!message) return null;

    const normalizedResults = message.parts.flatMap((part) => {
      if (part.type !== "data-agent-tool-result") return [];
      const result = normalizeAgentToolResult(part.data);
      if (!result) return [];
      const view = describeAgentToolResult(result.name, result.result);
      return [{ result, view }];
    });

    // presentation 是模型明确提交的最终推荐清单。若一轮里因重试调用多次，
    // 只认最后一次调用本身，而不是“最后一次成功调用”。后一次失败时不能
    // 悄悄回退到旧成功清单，让用户误以为失败的调整已经生效。
    let lastPresentation: (typeof normalizedResults)[number] | undefined;
    for (let index = normalizedResults.length - 1; index >= 0; index -= 1) {
      if (normalizedResults[index]?.result.name === "present_website_recommendations") {
        lastPresentation = normalizedResults[index];
        break;
      }
    }
    const finalPresentation = lastPresentation?.view.kind === "links"
      ? lastPresentation as (typeof normalizedResults)[number] & {
          view: Extract<(typeof normalizedResults)[number]["view"], { kind: "links" }>;
        }
      : undefined;
    const metadata = normalizeAgentMessageMetadata(message.metadata);
    const collectionDisabled =
      (metadata.messageStatus !== undefined && metadata.messageStatus !== "complete") ||
      metadata.turnPersisted === false;
    if (
      (!finalPresentation && (metadata.recommendationManifestVersion ?? 0) >= 1) ||
      (lastPresentation !== undefined && finalPresentation === undefined)
    ) {
      return null;
    }
    const visibleResults = finalPresentation
      ? [finalPresentation]
      : normalizedResults.filter(
          (entry): entry is (typeof normalizedResults)[number] & {
            view: Extract<(typeof normalizedResults)[number]["view"], { kind: "links" }>;
          } => entry.view.kind === "links",
        );
    const resultKey = visibleResults.map(({ result }) => result.toolCallId).join(":");
    const seen = new Set<string>();
    const library: AgentToolLink[] = [];
    let libraryTotal = 0;
    const web: AgentToolLink[] = [];
    let provider: string | null = null;
    for (const { result, view } of visibleResults) {
      if (
        view.source !== AGENT_SOURCE_LIBRARY &&
        view.source !== AGENT_SOURCE_WEB &&
        view.source !== AGENT_SOURCE_MODEL
      ) continue;
      if (view.source === AGENT_SOURCE_WEB || view.source === AGENT_SOURCE_MODEL) {
        provider =
          provider ??
          readWebProvider(result.result) ??
          (view.source === AGENT_SOURCE_MODEL ? AGENT_SOURCE_MODEL : null);
      }
      for (const item of view.items) {
        const key = item.siteId ?? item.url ?? item.name;
        if (seen.has(key)) continue;
        seen.add(key);
        if (item.siteId) {
          library.push(item);
          libraryTotal += 1;
        } else {
          web.push(item);
        }
      }
      if (view.source === AGENT_SOURCE_LIBRARY) {
        // matched_count 是命中总数，可能大于返回条数，分组标题以它为准
        libraryTotal += Math.max(0, (view.matchedCount ?? view.items.length) - view.items.length);
      }
    }
    return library.length > 0 || web.length > 0
      ? {
          library: { key: `library:${resultKey}`, items: library, total: libraryTotal },
          web: { key: `web:${resultKey}`, items: web, provider, collectionDisabled },
        }
      : null;
  }, [messages, status]);

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
          ? `已检索网址库 · ${stage.count ?? 0} 条匹配`
          : "正在检索网址库…"
        : stage.tool === "web_search"
          ? stage.status === "done"
            ? `已联网搜索${stage.provider ? ` · ${stage.provider}` : ""} · ${stage.count ?? 0} 个来源`
            : "正在联网搜索…"
          : stage.status === "done"
            ? `已完成：${agentToolLabel(stage.tool)}`
            : `正在${agentToolLabel(stage.tool)}…`,
  }));
  const stagedCallIds = new Set(stageItems.map((item) => item.key));
  for (const call of activeToolCalls) {
    if (stagedCallIds.has(call.toolCallId)) continue;
    stageItems.push({
      key: call.toolCallId,
      label: `正在${agentToolLabel(call.name)}…`,
      done: false,
    });
  }
  const hasActiveStage = stageItems.some((item) => !item.done);
  if (answerStreaming && activeToolCalls.length === 0) {
    stageItems.push({ key: "finalize", label: "正在整理结果…", done: false });
  } else if (busy && !hasActiveStage) {
    stageItems.push({ key: "thinking", label: "正在思考…", done: false });
  }

  const activeCommandOptionId = commandPanelOpen && filteredCommands.length > 0
    ? `${commandPanelId}-option-${commandIndex}`
    : undefined;
  const commandPanel = commandPanelOpen ? (
    <div
      className="agent-command-panel"
      id={commandPanelId}
      role="listbox"
      aria-label="Slash 命令"
    >
      {filteredCommands.length > 0 ? (
        filteredCommands.map((command, index) => (
          <button
            type="button"
            role="option"
            id={`${commandPanelId}-option-${index}`}
            tabIndex={-1}
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

  return {
    activeToolCalls,
    activeCommandOptionId,
    answerStreaming,
    applyPrompt,
    busy,
    commandPanel,
    commandPanelId,
    commandPanelOpen,
    conversationId,
    conversationStarted,
    draftStates,
    draftWorkflowBusy,
    errorText,
    handleCollect,
    handleConfirmDraft,
    handleInputChange,
    handleInputKeyDown,
    handleSubmit,
    headerTime,
    headerTitle,
    historyGroups,
    handleHistoryMenuKeyDown,
    handleHistoryTriggerKeyDown,
    historyOpen,
    historyRef,
    historyStatus,
    historyTriggerRef,
    input,
    messages,
    openConversation,
    resultGroups,
    savedCount,
    scope,
    setScope,
    stageItems,
    startNewConversation,
    status,
    stop,
    streamError,
    textareaRef,
    toggleHistory,
    webSaves,
  };
}
