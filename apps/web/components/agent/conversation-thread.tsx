"use client";

import {
  ArrowRight,
  Bookmark,
  Check,
  CircleAlert,
  ExternalLink,
  Loader,
  PencilLine,
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
  type AgentDraftAction,
  type AgentSiteDraft,
  type AgentSiteBatchDraft,
  type AgentSiteUpdateDraft,
  type AgentReclassifyDraft,
  type AgentSpaceMembershipDraft,
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
  onConfirmDraft: (toolCallId: string, action: AgentDraftAction) => void;
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

/** 三类草稿共用的确认区：待确认 / 保存中 / 已生效 / 失败四态都在这里。 */
function DraftActions({
  state,
  icon,
  idleLabel,
  busyLabel,
  doneLabel,
  onConfirm,
}: Readonly<{
  state: AgentDraftState;
  icon: React.ReactNode;
  idleLabel: string;
  busyLabel: string;
  doneLabel: string;
  onConfirm: () => void;
}>) {
  return (
    <>
      <div className="draft-card-actions">
        {state.status === "saved" ? (
          <span className="draft-card-done">
            <Check aria-hidden="true" />
            {doneLabel}
          </span>
        ) : (
          <button
            type="button"
            className="draft-confirm-button"
            disabled={state.status === "saving"}
            onClick={onConfirm}
          >
            {state.status === "saving" ? <Loader className="spin" aria-hidden="true" /> : icon}
            {state.status === "saving" ? busyLabel : idleLabel}
          </button>
        )}
        <span className="draft-card-hint">Agent 不会自行写入，需要你确认。</span>
      </div>
      {state.status === "error" && state.message && (
        <p className="draft-card-error">{state.message}</p>
      )}
    </>
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
  onConfirm: (toolCallId: string, action: AgentDraftAction) => void;
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
      <DraftActions
        state={state}
        icon={<Bookmark aria-hidden="true" />}
        idleLabel="确认保存"
        busyLabel="保存中…"
        doneLabel="已保存到资料库"
        onConfirm={() => onConfirm(toolCallId, { kind: "site", draft })}
      />
    </div>
  );
}

const UPDATE_FIELD_LABELS = {
  name: "名称",
  description: "说明",
  category: "分类",
  tags: "标签",
  pinned: "置顶",
} as const;

function updateFieldText(
  field: keyof typeof UPDATE_FIELD_LABELS,
  site: AgentToolLink,
): string {
  if (field === "name") return site.name;
  if (field === "description") return site.description || "（空）";
  if (field === "category") return site.category || "（无）";
  if (field === "tags") return site.tags.length > 0 ? site.tags.join("、") : "（无）";
  return site.pinned ? "是" : "否";
}

function SiteUpdateCard({
  toolCallId,
  draft,
  state,
  onConfirm,
}: Readonly<{
  toolCallId: string;
  draft: AgentSiteUpdateDraft;
  state: AgentDraftState;
  onConfirm: (toolCallId: string, action: AgentDraftAction) => void;
}>) {
  // 只列出草稿真的要改的字段：把没变的字段也排进 diff 只会淹没重点。
  const fields = (Object.keys(UPDATE_FIELD_LABELS) as (keyof typeof UPDATE_FIELD_LABELS)[]).filter(
    (field) => draft.changes[field] !== undefined,
  );

  return (
    <div className="draft-card" data-variant="update">
      <div className="draft-card-main">
        <strong>{draft.before.name}</strong>
        {draft.before.url && (
          <a href={draft.before.url} target="_blank" rel="noreferrer noopener">
            {hostOf(draft.before.url)}
            <ExternalLink aria-hidden="true" />
          </a>
        )}
      </div>
      <dl className="draft-diff">
        {fields.map((field) => (
          <div className="draft-diff-row" key={field}>
            <dt>{UPDATE_FIELD_LABELS[field]}</dt>
            <dd>
              <span className="draft-diff-before">{updateFieldText(field, draft.before)}</span>
              <ArrowRight aria-hidden="true" />
              <span className="draft-diff-after">{updateFieldText(field, draft.after)}</span>
            </dd>
          </div>
        ))}
      </dl>
      <DraftActions
        state={state}
        icon={<PencilLine aria-hidden="true" />}
        idleLabel="确认修改"
        busyLabel="修改中…"
        doneLabel="修改已生效"
        onConfirm={() => onConfirm(toolCallId, { kind: "site_update", draft })}
      />
    </div>
  );
}

const BATCH_STATUS_LABELS = {
  ready: "将新增",
  duplicate: "已存在",
  invalid: "无法识别",
} as const;

function SiteBatchCard({
  toolCallId,
  draft,
  state,
  onConfirm,
}: Readonly<{
  toolCallId: string;
  draft: AgentSiteBatchDraft;
  state: AgentDraftState;
  onConfirm: (toolCallId: string, action: AgentDraftAction) => void;
}>) {
  return (
    <div className="draft-card" data-variant="batch">
      <p className="draft-card-description">
        共 {draft.total} 个网址：将新增 <strong>{draft.ready}</strong> 个
        {draft.duplicate > 0 && <>，{draft.duplicate} 个已存在</>}
        {draft.invalid > 0 && <>，{draft.invalid} 个无法识别</>}
        。已存在与无法识别的都会跳过，不会重复写入。
      </p>
      <ul className="draft-batch-list">
        {draft.items.map((item) => (
          <li key={item.url} data-status={item.status}>
            <span className="draft-batch-status">{BATCH_STATUS_LABELS[item.status]}</span>
            <span className="draft-batch-url" title={item.url}>{item.url}</span>
          </li>
        ))}
      </ul>
      <DraftActions
        state={state}
        icon={<Bookmark aria-hidden="true" />}
        idleLabel={`确认收录 ${draft.ready} 个`}
        busyLabel="收录中…"
        doneLabel="已批量收录"
        onConfirm={() => onConfirm(toolCallId, { kind: "site_batch", draft })}
      />
    </div>
  );
}

function SpaceMembershipCard({
  toolCallId,
  draft,
  state,
  onConfirm,
}: Readonly<{
  toolCallId: string;
  draft: AgentSpaceMembershipDraft;
  state: AgentDraftState;
  onConfirm: (toolCallId: string, action: AgentDraftAction) => void;
}>) {
  const adding = draft.action === "add";
  return (
    <div className="draft-card" data-variant="membership">
      <div className="draft-card-main">
        <strong>{draft.siteName}</strong>
      </div>
      <p className="draft-card-description">
        {adding ? "将加入 Space" : "将移出 Space"}「{draft.spaceName}」
        {adding ? "。" : "，网站本身仍保留在资料库中。"}
      </p>
      <DraftActions
        state={state}
        icon={<PencilLine aria-hidden="true" />}
        idleLabel={adding ? "确认加入" : "确认移出"}
        busyLabel="处理中…"
        doneLabel={adding ? "已加入 Space" : "已移出 Space"}
        onConfirm={() => onConfirm(toolCallId, { kind: "space_membership", draft })}
      />
    </div>
  );
}

function ReclassifyCard({
  toolCallId,
  draft,
  state,
  onConfirm,
}: Readonly<{
  toolCallId: string;
  draft: AgentReclassifyDraft;
  state: AgentDraftState;
  onConfirm: (toolCallId: string, action: AgentDraftAction) => void;
}>) {
  return (
    <div className="draft-card" data-variant="reclassify">
      <div className="draft-card-main">
        <strong>全库重分类</strong>
      </div>
      {/* 花钱提示：重分类要调用户自己的 model Provider，确认前必须先看到预估请求数。 */}
      <p className="draft-card-description">
        将重新分类 <strong>{draft.siteCount}</strong> 个网站，预计发出{" "}
        <strong>{draft.estimatedRequestCount}</strong> 次模型请求（约{" "}
        {draft.estimatedInputCharacters.toLocaleString("zh-CN")} 字符输入）。
        费用记在你自己配置的 Provider 上，不确认不会消耗任何额度。
      </p>
      {draft.allowedCategories.length > 0 && (
        <div className="tool-chip-row">
          {draft.allowedCategories.map((name) => (
            <span className="tool-chip" key={name}>{name}</span>
          ))}
        </div>
      )}
      <p className="draft-card-description">
        只会归入上面这些已有分类，模型不能自行新建分类。
      </p>
      <DraftActions
        state={state}
        icon={<PencilLine aria-hidden="true" />}
        idleLabel="确认开始重分类"
        busyLabel="重分类中…"
        doneLabel="已完成重分类"
        onConfirm={() => onConfirm(toolCallId, { kind: "reclassify", draft })}
      />
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
  onConfirmDraft: (toolCallId: string, action: AgentDraftAction) => void;
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
      {view.kind === "site-update" && (
        <SiteUpdateCard
          toolCallId={result.toolCallId}
          draft={view.draft}
          state={draftState}
          onConfirm={onConfirmDraft}
        />
      )}
      {view.kind === "site-batch" && (
        <SiteBatchCard
          toolCallId={result.toolCallId}
          draft={view.draft}
          state={draftState}
          onConfirm={onConfirmDraft}
        />
      )}
      {view.kind === "space-membership" && (
        <SpaceMembershipCard
          toolCallId={result.toolCallId}
          draft={view.draft}
          state={draftState}
          onConfirm={onConfirmDraft}
        />
      )}
      {view.kind === "reclassify" && (
        <ReclassifyCard
          toolCallId={result.toolCallId}
          draft={view.draft}
          state={draftState}
          onConfirm={onConfirmDraft}
        />
      )}
      {view.kind === "noop" && <p className="tool-card-note">{view.message}</p>}
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
