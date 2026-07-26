"use client";

import {
  Bookmark,
  Check,
  CircleAlert,
  ExternalLink,
  Loader,
  Sparkles,
  User,
  Wrench,
} from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import Link from "next/link";
import { useEffect, useRef } from "react";

import { ShinyText } from "@/components/react-bits/shiny-text";
import {
  agentSourceLabels,
  agentToolLabel,
  describeAgentToolResult,
  normalizeAgentToolResult,
  type AgentSiteDraft,
  type AgentToolCall,
  type AgentToolLink,
  type AgentToolResult,
  type AgentUIMessage,
} from "@/lib/agent-contract";

export type AgentDraftState = {
  status: "idle" | "saving" | "saved" | "error";
  message?: string;
};

const IDLE_DRAFT: AgentDraftState = { status: "idle" };

type ConversationThreadProps = {
  messages: readonly AgentUIMessage[];
  status: "submitted" | "streaming" | "ready" | "error";
  activeToolCalls: readonly AgentToolCall[];
  draftStates: Readonly<Record<string, AgentDraftState>>;
  onConfirmDraft: (toolCallId: string, draft: AgentSiteDraft) => void;
  errorText: string | null;
  errorCode: string | null;
};

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function ChipRow({
  category,
  tags,
}: Readonly<{ category: string | null; tags: readonly string[] }>) {
  if (!category && tags.length === 0) return null;
  return (
    <div className="tool-chip-row">
      {category && (
        <span className="tool-chip" data-variant="category">
          {category}
        </span>
      )}
      {tags.map((tag) => (
        <span className="tool-chip" key={tag}>
          {tag}
        </span>
      ))}
    </div>
  );
}

function ToolLinkList({ items }: Readonly<{ items: readonly AgentToolLink[] }>) {
  return (
    <ul className="tool-links">
      {items.map((item, index) => (
        <li key={`${item.siteId ?? item.url ?? item.name}-${index}`}>
          {item.url ? (
            <a href={item.url} target="_blank" rel="noreferrer noopener">
              <span className="tool-link-name">{item.name}</span>
              <span className="tool-link-host">{hostOf(item.url)}</span>
              <ExternalLink aria-hidden="true" />
            </a>
          ) : (
            <span className="tool-link-name">{item.name}</span>
          )}
          {item.description && <p className="tool-link-description">{item.description}</p>}
          <ChipRow category={item.category} tags={item.tags} />
        </li>
      ))}
    </ul>
  );
}

function DraftCard({
  toolCallId,
  draft,
  duplicate,
  state,
  onConfirm,
}: Readonly<{
  toolCallId: string;
  draft: AgentSiteDraft;
  duplicate: AgentToolLink | null;
  state: AgentDraftState;
  onConfirm: (toolCallId: string, draft: AgentSiteDraft) => void;
}>) {
  return (
    <div className="draft-card">
      <div className="draft-card-main">
        <strong>{draft.name}</strong>
        <a href={draft.url} target="_blank" rel="noreferrer noopener">
          {hostOf(draft.url)}
          <ExternalLink aria-hidden="true" />
        </a>
      </div>
      {draft.description && <p className="draft-card-description">{draft.description}</p>}
      <ChipRow category={draft.category || null} tags={draft.tags} />
      {duplicate && (
        <p className="draft-card-warning">
          <CircleAlert aria-hidden="true" />
          资料库里已有相似记录「{duplicate.name}」，确认后会新增一条。
        </p>
      )}
      <div className="draft-card-actions">
        {state.status === "saved" ? (
          <span className="draft-card-done">
            <Check aria-hidden="true" />
            已保存到资料库
          </span>
        ) : (
          <button
            type="button"
            className="draft-confirm-button"
            disabled={state.status === "saving"}
            onClick={() => onConfirm(toolCallId, draft)}
          >
            {state.status === "saving" ? (
              <Loader className="spin" aria-hidden="true" />
            ) : (
              <Bookmark aria-hidden="true" />
            )}
            {state.status === "saving" ? "保存中…" : "确认保存"}
          </button>
        )}
        <span className="draft-card-hint">Agent 不会自行写入，需要你确认。</span>
      </div>
      {state.status === "error" && state.message && (
        <p className="draft-card-error">{state.message}</p>
      )}
    </div>
  );
}

function ToolCard({
  result,
  draftState,
  onConfirmDraft,
}: Readonly<{
  result: AgentToolResult;
  draftState: AgentDraftState;
  onConfirmDraft: (toolCallId: string, draft: AgentSiteDraft) => void;
}>) {
  const view = describeAgentToolResult(result.name, result.result);
  const source = "source" in view ? view.source : null;

  return (
    <section className="tool-card" data-kind={view.kind}>
      <header className="tool-card-head">
        <Wrench aria-hidden="true" />
        <strong>{agentToolLabel(result.name)}</strong>
        {source && <span className="source-badge">来源：{source}</span>}
      </header>
      {view.kind === "links" &&
        (view.items.length > 0 ? (
          <>
            <ToolLinkList items={view.items} />
            {view.matchedCount !== null && view.matchedCount > view.items.length && (
              <p className="tool-card-note">
                共匹配 {view.matchedCount} 条，已展示前 {view.items.length} 条。
              </p>
            )}
          </>
        ) : (
          <p className="tool-card-note">没有命中任何结果。</p>
        ))}
      {view.kind === "facets" &&
        (view.items.length > 0 ? (
          <div className="tool-chip-row">
            {view.items.map((item) => (
              <span className="tool-chip" key={item.id}>
                {item.name}
                {item.count !== null && <em>{item.count}</em>}
              </span>
            ))}
          </div>
        ) : (
          <p className="tool-card-note">还没有任何记录。</p>
        ))}
      {view.kind === "draft" && (
        <DraftCard
          toolCallId={result.toolCallId}
          draft={view.draft}
          duplicate={view.duplicate}
          state={draftState}
          onConfirm={onConfirmDraft}
        />
      )}
      {view.kind === "rejected" && <p className="tool-card-note">{view.reason}</p>}
      {view.kind === "error" && (
        <p className="tool-card-note" data-tone="danger">
          <CircleAlert aria-hidden="true" />
          {view.message}
        </p>
      )}
      {view.kind === "raw" && <pre className="tool-card-raw">{view.text}</pre>}
    </section>
  );
}

function collectToolResults(message: AgentUIMessage): AgentToolResult[] {
  const results: AgentToolResult[] = [];
  for (const part of message.parts) {
    if (part.type !== "data-agent-tool-result") continue;
    const normalized = normalizeAgentToolResult(part.data);
    if (normalized) results.push(normalized);
  }
  return results;
}

export function ConversationThread({
  messages,
  status,
  activeToolCalls,
  draftStates,
  onConfirmDraft,
  errorText,
  errorCode,
}: Readonly<ConversationThreadProps>) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const reducedMotion = useReducedMotion();

  useEffect(() => {
    bottomRef.current?.scrollIntoView({
      block: "end",
      behavior: reducedMotion ? "auto" : "smooth",
    });
  }, [messages, status, reducedMotion]);

  const waiting = status === "submitted" || status === "streaming";
  const lastMessage = messages[messages.length - 1];
  // The placeholder turn only makes sense before the assistant bubble exists.
  const awaitingFirstToken = waiting && (lastMessage === undefined || lastMessage.role === "user");
  const pendingCall = activeToolCalls[activeToolCalls.length - 1];

  return (
    <div className="chat-thread" role="log" aria-live="polite" aria-busy={waiting}>
      {messages.map((message) => {
        if (message.role !== "user" && message.role !== "assistant") return null;
        const toolResults = message.role === "assistant" ? collectToolResults(message) : [];
        const hasText = message.parts.some((part) => part.type === "text" && part.text.trim());

        return (
          <motion.article
            className="chat-turn"
            data-role={message.role}
            key={message.id}
            initial={reducedMotion ? false : { opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: reducedMotion ? 0 : 0.24, ease: "easeOut" }}
          >
            <span className="chat-avatar" aria-hidden="true">
              {message.role === "user" ? <User /> : <Sparkles />}
            </span>
            <div className="chat-turn-body">
              {message.parts.map((part, index) => {
                if (part.type === "text") {
                  return part.text ? (
                    <p className="chat-text" key={`text-${index}`}>
                      {part.text}
                    </p>
                  ) : null;
                }
                if (part.type === "data-agent-tool-result") {
                  const result = normalizeAgentToolResult(part.data);
                  if (!result) return null;
                  return (
                    <ToolCard
                      key={`tool-${result.toolCallId}-${index}`}
                      result={result}
                      draftState={draftStates[result.toolCallId] ?? IDLE_DRAFT}
                      onConfirmDraft={onConfirmDraft}
                    />
                  );
                }
                return null;
              })}
              {message.role === "assistant" && hasText && (
                <div className="chat-sources">
                  {agentSourceLabels(toolResults).map((label) => (
                    <span className="source-badge" key={label}>
                      来源：{label}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </motion.article>
        );
      })}

      {awaitingFirstToken && (
        <article className="chat-turn" data-role="assistant" data-pending="true">
          <span className="chat-avatar" aria-hidden="true">
            <Sparkles />
          </span>
          <div className="chat-turn-body">
            <p className="chat-pending">
              <ShinyText
                text={pendingCall ? `正在${agentToolLabel(pendingCall.name)}…` : "正在思考…"}
              />
            </p>
          </div>
        </article>
      )}

      {errorText && (
        <p className="chat-error" role="alert">
          <CircleAlert aria-hidden="true" />
          {errorText}
          {errorCode === "provider_not_configured" && (
            <Link className="chat-error-action" href="/settings/providers">
              去配置 Provider
            </Link>
          )}
        </p>
      )}
      <div ref={bottomRef} />
    </div>
  );
}
