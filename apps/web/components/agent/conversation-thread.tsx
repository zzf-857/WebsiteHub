"use client";

import {
  ArrowRight,
  Brain,
  Bookmark,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock,
  ExternalLink,
  FolderInput,
  FolderPlus,
  Hash,
  Loader,
  PencilLine,
  RotateCcw,
  Sparkles,
  User,
  Wrench,
  X,
} from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Streamdown, type Components } from "streamdown";

import { ShinyText } from "@/components/react-bits/shiny-text";
import { SiteFavicon } from "@/components/site-favicon";
import {
  agentSourceLabels,
  agentToolLabel,
  describeAgentToolResult,
  normalizeAgentMarkdownLink,
  normalizeAgentMessageMetadata,
  normalizeAgentToolResult,
  type AgentDraftAction,
  type AgentSiteDraft,
  type AgentSiteBatchDraft,
  type AgentSiteUpdateDraft,
  type AgentReclassifyDraft,
  type AgentSpaceBatchDraft,
  type AgentSpaceMembershipDraft,
  type AgentToolCall,
  type AgentToolLink,
  type AgentToolResult,
  type AgentMessageMetadata,
  type AgentUIMessage,
} from "@/lib/agent-contract";

export type AgentDraftState = {
  status: "idle" | "saving" | "saved" | "error";
  message?: string;
  confirmationPending?: boolean;
  blocksConversation?: boolean;
};

const IDLE_DRAFT: AgentDraftState = { status: "idle" };
const SPACE_BATCH_EXCLUSIONS_PREFIX = "webhub:agent-space-batch-exclusions:";
const SPACE_BATCH_ATTEMPTED_PREFIX = "webhub:agent-space-batch-attempted:";

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

function restoredSpaceBatchExclusions(
  storageKey: string,
  sites: readonly (AgentSpaceBatchDraft["sites"][number])[],
): ReadonlySet<string> {
  try {
    const stored = JSON.parse(
      window.sessionStorage.getItem(`${SPACE_BATCH_EXCLUSIONS_PREFIX}${storageKey}`) ?? "[]",
    );
    if (!Array.isArray(stored)) return new Set<string>();
    const allowed = new Set(sites.map((site) => site.siteId));
    return new Set(
      stored.filter((siteId): siteId is string =>
        typeof siteId === "string" && allowed.has(siteId)),
    );
  } catch {
    return new Set<string>();
  }
}

function formatDuration(milliseconds: number): string {
  if (milliseconds < 1_000) return `${milliseconds} 毫秒`;
  const seconds = milliseconds / 1_000;
  return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} 秒`;
}

function MarkdownAnchor({ href, children, title }: React.ComponentProps<"a"> & { node?: unknown }) {
  const link = normalizeAgentMarkdownLink(href);
  if (link === null) return <span>{children}</span>;
  if (link.kind === "internal") {
    return (
      <Link className="chat-inline-link" href={link.href} title={title}>
        {children}
      </Link>
    );
  }
  return (
    <a
      className="chat-link-card"
      href={link.href}
      target="_blank"
      rel="noreferrer noopener"
      title={title}
    >
      <span className="chat-link-label">{children}</span>
      <span className="chat-link-host">
        {link.hostname}
        <ExternalLink aria-hidden="true" />
      </span>
    </a>
  );
}

const MARKDOWN_COMPONENTS = { a: MarkdownAnchor } as Components;
const STREAM_DISALLOWED_ELEMENTS = ["img"];
const STREAM_ANIMATION = { animation: "fadeIn", duration: 140 } as const;
const STREAM_CONTROLS = {
  code: { copy: true, download: false },
  table: false,
  mermaid: false,
} as const;
const STREAM_TRANSLATIONS = {
  copyCode: "复制代码",
  copied: "已复制",
  openExternalLink: "打开外部链接",
} as const;

function AgentMarkdown({
  text,
  streaming,
  reducedMotion,
  className,
}: Readonly<{
  text: string;
  streaming: boolean;
  reducedMotion: boolean;
  className: string;
}>) {
  return (
    <Streamdown
      className={className}
      mode={streaming ? "streaming" : "static"}
      isAnimating={streaming && !reducedMotion}
      animated={streaming && !reducedMotion ? STREAM_ANIMATION : false}
      caret={streaming ? "block" : undefined}
      components={MARKDOWN_COMPONENTS}
      disallowedElements={STREAM_DISALLOWED_ELEMENTS}
      controls={STREAM_CONTROLS}
      translations={STREAM_TRANSLATIONS}
    >
      {text}
    </Streamdown>
  );
}

function ReasoningDisclosure({
  text,
  streaming,
  durationMs,
  reducedMotion,
}: Readonly<{
  text: string;
  streaming: boolean;
  durationMs?: number;
  reducedMotion: boolean;
}>) {
  const initializeDetails = useCallback((element: HTMLDetailsElement | null) => {
    if (element && streaming) element.open = true;
  }, [streaming]);

  return (
    <details
      className="chat-reasoning"
      ref={initializeDetails}
    >
      <summary>
        <Brain aria-hidden="true" />
        <span>{streaming ? "思考中" : "思考过程"}</span>
        {!streaming && durationMs !== undefined && (
          <span className="chat-reasoning-duration">{formatDuration(durationMs)}</span>
        )}
        <ChevronDown className="chat-reasoning-chevron" aria-hidden="true" />
      </summary>
      <AgentMarkdown
        className="chat-markdown chat-reasoning-content"
        text={text}
        streaming={streaming}
        reducedMotion={reducedMotion}
      />
    </details>
  );
}

function ResponseMetrics({ metadata }: Readonly<{ metadata: AgentMessageMetadata }>) {
  const usage = metadata.usage;
  const hasUsage = usage !== undefined && Object.keys(usage).length > 0;
  if (metadata.elapsedMs === undefined && metadata.timeToFirstTokenMs === undefined && !hasUsage) {
    return null;
  }
  const usageTitle = usage
    ? [
        usage.inputTokens !== undefined ? `输入 ${usage.inputTokens.toLocaleString("zh-CN")}` : null,
        usage.outputTokens !== undefined ? `输出 ${usage.outputTokens.toLocaleString("zh-CN")}` : null,
        usage.reasoningTokens !== undefined
          ? `思考 ${usage.reasoningTokens.toLocaleString("zh-CN")}`
          : null,
      ].filter(Boolean).join(" · ")
    : "";
  return (
    <div className="chat-response-metrics" aria-label="本次回答统计">
      {metadata.elapsedMs !== undefined && (
        <span>
          <Clock aria-hidden="true" />
          用时 {formatDuration(metadata.elapsedMs)}
        </span>
      )}
      {metadata.timeToFirstTokenMs !== undefined && (
        <span title="从服务端收到请求到首个模型输出">
          首字 {formatDuration(metadata.timeToFirstTokenMs)}
        </span>
      )}
      {usage && usage.totalTokens !== undefined && (
        <span title={usageTitle || undefined}>
          <Hash aria-hidden="true" />
          {usage.totalTokens.toLocaleString("zh-CN")} Token
        </span>
      )}
      {usage && usage.totalTokens === undefined && usageTitle && (
        <span title="Provider 未返回总量，仅展示其实际返回的分项">
          <Hash aria-hidden="true" />
          {usageTitle}
        </span>
      )}
    </div>
  );
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
      {items.map((item, index) => {
        const cardDescription = item.summary || item.description;
        const content = (
          <>
            <SiteFavicon url={item.faviconUrl} name={item.name} size={24} />
            <span className="tool-link-copy">
              <span className="tool-link-name">{item.name}</span>
              {item.url && <span className="tool-link-host">{hostOf(item.url)}</span>}
            </span>
            <ExternalLink aria-hidden="true" />
          </>
        );
        return (
          <li key={`${item.siteId ?? item.url ?? item.name}-${index}`}>
            {item.siteId ? (
              <Link href={`/library/${encodeURIComponent(item.siteId)}`}>{content}</Link>
            ) : item.url ? (
              <a href={item.url} target="_blank" rel="noreferrer noopener">{content}</a>
            ) : (
              <span className="tool-link-static">{content}</span>
            )}
          {cardDescription && <p className="tool-link-description">{cardDescription}</p>}
          <ChipRow category={item.category} tags={item.tags} />
          </li>
        );
      })}
    </ul>
  );
}

/** 所有草稿共用的确认区：待确认 / 保存中 / 已生效 / 失败四态都在这里。 */
function DraftActions({
  state,
  icon,
  idleLabel,
  busyLabel,
  doneLabel,
  disabled = false,
  disabledLabel,
  hint = "Agent 不会自行写入，需要你确认。",
  onConfirm,
}: Readonly<{
  state: AgentDraftState;
  icon: React.ReactNode;
  idleLabel: string;
  busyLabel: string;
  doneLabel: string;
  disabled?: boolean;
  disabledLabel?: string;
  hint?: string;
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
            disabled={state.status === "saving" || disabled}
            onClick={onConfirm}
          >
            {state.status === "saving" ? <Loader className="spin" aria-hidden="true" /> : icon}
            {state.status === "saving"
              ? busyLabel
              : disabled && disabledLabel
                ? disabledLabel
                : idleLabel}
          </button>
        )}
        <span className="draft-card-hint">{hint}</span>
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
          网址库里已有相似记录「{duplicate.name}」，确认后会新增一条。
        </p>
      )}
      <DraftActions
        state={state}
        icon={<Bookmark aria-hidden="true" />}
        idleLabel="确认保存"
        busyLabel="保存中…"
        doneLabel="已保存到网址库"
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
        {draft.before.siteId ? (
          <Link href={`/library/${encodeURIComponent(draft.before.siteId)}`}>
            {draft.before.url ? hostOf(draft.before.url) : "查看网站详情"}
            <ChevronRight aria-hidden="true" />
          </Link>
        ) : draft.before.url ? (
          <a href={draft.before.url} target="_blank" rel="noreferrer noopener">
            {hostOf(draft.before.url)}
            <ExternalLink aria-hidden="true" />
          </a>
        ) : null}
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
        {adding && state.status === "saved" ? (
          <>已加入 Space「{draft.spaceName}」。</>
        ) : adding ? (
          <>
            这是一份旧版逐项加入草稿。请重新告诉 Agent 加入 Space「{draft.spaceName}」，
            它会生成一张可一次确认的批量任务。
          </>
        ) : (
          <>将移出 Space「{draft.spaceName}」，网站本身仍保留在网址库中。</>
        )}
      </p>
      <DraftActions
        state={state}
        icon={<PencilLine aria-hidden="true" />}
        idleLabel="确认移出"
        busyLabel="处理中…"
        doneLabel={adding ? "已加入 Space" : "已移出 Space"}
        disabled={adding}
        disabledLabel="旧版加入草稿已失效"
        hint={adding && state.status !== "saved" ? "重新发起后只需确认一次。" : undefined}
        onConfirm={() => onConfirm(toolCallId, { kind: "space_membership", draft })}
      />
    </div>
  );
}

function SpaceBatchCard({
  storageScope,
  toolCallId,
  draft,
  state,
  superseded,
  messagePersisted,
  agentWaiting,
  onConfirm,
}: Readonly<{
  storageScope: string;
  toolCallId: string;
  draft: AgentSpaceBatchDraft;
  state: AgentDraftState;
  superseded: boolean;
  messagePersisted: boolean;
  agentWaiting: boolean;
  onConfirm: (toolCallId: string, action: AgentDraftAction) => void;
}>) {
  const storageKey = `${storageScope}:${toolCallId}`;
  const [excludedSiteIds, setExcludedSiteIds] = useState<ReadonlySet<string>>(
    new Set<string>(),
  );
  const [attempted, setAttempted] = useState(false);
  const [storageReady, setStorageReady] = useState(false);
  const initialSitesRef = useRef(draft.sites);
  useEffect(() => {
    setExcludedSiteIds(restoredSpaceBatchExclusions(storageKey, initialSitesRef.current));
    try {
      setAttempted(
        window.sessionStorage.getItem(`${SPACE_BATCH_ATTEMPTED_PREFIX}${storageKey}`) === "1",
      );
    } catch {
      setAttempted(false);
    }
    setStorageReady(true);
  }, [storageKey]);
  useEffect(() => {
    if (!storageReady) return;
    try {
      const key = `${SPACE_BATCH_EXCLUSIONS_PREFIX}${storageKey}`;
      if (excludedSiteIds.size === 0) window.sessionStorage.removeItem(key);
      else window.sessionStorage.setItem(key, JSON.stringify([...excludedSiteIds]));
    } catch {
      // Storage may be disabled; the current mounted card still keeps its selection.
    }
  }, [excludedSiteIds, storageKey, storageReady]);
  const selectedSites = useMemo(
    () => draft.sites.filter((site) => !excludedSiteIds.has(site.siteId)),
    [draft.sites, excludedSiteIds],
  );
  const replaced = superseded && state.status !== "saving" && state.status !== "saved";
  const editable = storageReady && messagePersisted && !agentWaiting && !attempted && !replaced &&
    (state.status === "idle" || state.status === "error");
  const existingTargetIsEmpty = draft.target.mode === "existing" && selectedSites.length === 0;
  const creatingEmptySpace = draft.target.mode === "create" && selectedSites.length === 0;

  const toggleSite = (siteId: string) => {
    if (!editable) return;
    setExcludedSiteIds((current) => {
      const next = new Set(current);
      if (next.has(siteId)) next.delete(siteId);
      else next.add(siteId);
      return next;
    });
  };

  const idleLabel = draft.target.mode === "create"
    ? creatingEmptySpace
      ? "确认创建 Space"
      : `确认创建并加入 ${selectedSites.length} 个`
    : `确认执行 ${selectedSites.length} 个`;

  return (
    <div className="draft-card" data-variant="space-batch">
      <div className="draft-space-target">
        <span className="draft-space-target-icon" aria-hidden="true">
          {draft.target.mode === "create" ? <FolderPlus /> : <FolderInput />}
        </span>
        <span className="draft-space-target-copy">
          <small>{draft.target.mode === "create" ? "新建 Space" : "加入已有 Space"}</small>
          <strong>{draft.target.spaceName}</strong>
        </span>
        {draft.sites.length > 0 && (
          <span className="draft-space-count">
            已选择 {selectedSites.length}/{draft.sites.length}
          </span>
        )}
      </div>

      {draft.sites.length > 0 ? (
        <>
          <p className="draft-card-description">
            本次将按以下候选清单整体执行。
          </p>
          <ul className="draft-space-sites">
            {draft.sites.map((site) => {
              const excluded = excludedSiteIds.has(site.siteId);
              return (
                <li key={site.siteId} data-excluded={excluded || undefined}>
                  <Link
                    href={`/library/${encodeURIComponent(site.siteId)}`}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    <span className="draft-space-site-name">{site.name}</span>
                    <span className="draft-space-site-host">{hostOf(site.url)}</span>
                  </Link>
                  <button
                    type="button"
                    className="draft-space-site-toggle"
                    disabled={!editable}
                    aria-label={excluded ? `恢复 ${site.name}` : `剔除 ${site.name}`}
                    title={excluded ? "恢复候选" : "从本次任务中剔除"}
                    onClick={() => toggleSite(site.siteId)}
                  >
                    {excluded ? <RotateCcw aria-hidden="true" /> : <X aria-hidden="true" />}
                  </button>
                </li>
              );
            })}
          </ul>
          {excludedSiteIds.size > 0 && editable && (
            <button
              type="button"
              className="draft-space-restore-all"
              onClick={() => setExcludedSiteIds(new Set<string>())}
            >
              <RotateCcw aria-hidden="true" />
              恢复全部候选
            </button>
          )}
        </>
      ) : (
        <p className="draft-card-description">这次任务只会创建 Space，不会加入网站。</p>
      )}

      {draft.alreadyMemberCount > 0 && (
        <p className="draft-card-hint">
          原始候选中有 {draft.alreadyMemberCount} 个网站已经在该 Space 中，确认时会自动跳过。
        </p>
      )}
      {creatingEmptySpace && draft.sites.length > 0 && (
        <p className="draft-card-warning">
          <CircleAlert aria-hidden="true" />
          当前候选已全部剔除，确认后只创建 Space。
        </p>
      )}
      {replaced && (
        <p className="draft-card-superseded">
          这份草稿已被后续方案替代，请确认对话中最新的 Space 任务。
        </p>
      )}
      <DraftActions
        state={state}
        icon={draft.target.mode === "create"
          ? <FolderPlus aria-hidden="true" />
          : <FolderInput aria-hidden="true" />}
        idleLabel={state.confirmationPending
          ? "重试同步状态"
          : attempted
            ? "重试原任务"
            : idleLabel}
        busyLabel="正在执行整批任务…"
        doneLabel="Space 任务已完成"
        disabled={
          !storageReady || replaced || existingTargetIsEmpty || !messagePersisted || agentWaiting
        }
        disabledLabel={!storageReady
          ? "正在恢复任务状态"
          : replaced
          ? "已被后续方案替代"
          : agentWaiting
            ? "等待回答完成"
            : !messagePersisted
              ? "本轮未完整保存"
            : "至少保留 1 个网站"}
        hint={state.confirmationPending
          ? "网站与 Space 已处理；重试只会同步对话状态。"
          : attempted && state.status === "error"
          ? "任务载荷已锁定；如需改变候选，请让 Agent 生成新方案。"
          : replaced
          ? "旧方案不会再执行。"
          : agentWaiting
            ? "回答完成后即可一次确认。"
            : !messagePersisted
              ? "本轮任务没有完整保存，请让 Agent 重新生成。"
            : "等待确认后整体执行。"}
        onConfirm={() => {
          if (!storageReady) return;
          try {
            const exclusionsKey = `${SPACE_BATCH_EXCLUSIONS_PREFIX}${storageKey}`;
            if (excludedSiteIds.size === 0) {
              window.sessionStorage.removeItem(exclusionsKey);
            } else {
              window.sessionStorage.setItem(
                exclusionsKey,
                JSON.stringify([...excludedSiteIds]),
              );
            }
            window.sessionStorage.setItem(
              `${SPACE_BATCH_ATTEMPTED_PREFIX}${storageKey}`,
              "1",
            );
          } catch {
            // The mounted card still freezes its payload when storage is unavailable.
          }
          setAttempted(true);
          onConfirm(toolCallId, {
            kind: "space_batch",
            draft,
            selectedSiteIds: selectedSites.map((site) => site.siteId),
          });
        }}
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
        将重新分类 <strong>{draft.siteCount}</strong> 个网站，模型请求预计{" "}
        <strong>{draft.estimatedRequestCount}</strong> 次，单批失败最多重试一次，上限{" "}
        <strong>{draft.maximumRequestCount}</strong> 次（约{" "}
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
  storageScope,
  result,
  draftState,
  spaceBatchSuperseded,
  messagePersisted,
  agentWaiting,
  onConfirmDraft,
}: Readonly<{
  storageScope: string;
  result: AgentToolResult;
  draftState: AgentDraftState;
  spaceBatchSuperseded: boolean;
  messagePersisted: boolean;
  agentWaiting: boolean;
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
      {view.kind === "space-batch" && (
        <SpaceBatchCard
          storageScope={storageScope}
          toolCallId={result.toolCallId}
          draft={view.draft}
          state={draftState}
          superseded={spaceBatchSuperseded}
          messagePersisted={messagePersisted}
          agentWaiting={agentWaiting}
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
      {view.kind === "unavailable" && (
        <p className="tool-card-note">
          <CircleAlert aria-hidden="true" />
          {view.message}
        </p>
      )}
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
    // Message snapshots can update many times per second. Coalesce scroll work
    // to one paint and avoid restarting a smooth-scroll animation for every token.
    const frame = window.requestAnimationFrame(() => {
      bottomRef.current?.scrollIntoView({
        block: "end",
        behavior: reducedMotion || status === "streaming" ? "auto" : "smooth",
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages, status, reducedMotion]);

  const waiting = status === "submitted" || status === "streaming";
  const lastMessage = messages[messages.length - 1];
  // The placeholder turn only makes sense before the assistant bubble exists.
  const awaitingFirstToken = waiting && (lastMessage === undefined || lastMessage.role === "user");
  const pendingCall = activeToolCalls[activeToolCalls.length - 1];
  const latestSpaceBatchToolCallId = useMemo(() => {
    let latest: string | null = null;
    for (const message of messages) {
      if (message.role !== "assistant") continue;
      for (const part of message.parts) {
        if (part.type !== "data-agent-tool-result") continue;
        const result = normalizeAgentToolResult(part.data);
        if (!result) continue;
        // A later batch attempt also supersedes the previous plan when it
        // resolves to noop/rejected (for example, the user removed every item).
        if (result.name === "propose_space_batch") {
          latest = result.toolCallId;
        }
      }
    }
    return latest;
  }, [messages]);

  return (
    <div className="chat-thread" role="log" aria-live="polite" aria-busy={waiting}>
      {messages.map((message) => {
        if (message.role !== "user" && message.role !== "assistant") return null;
        const toolResults = message.role === "assistant" ? collectToolResults(message) : [];
        const hasText = message.parts.some((part) => part.type === "text" && part.text.trim());
        const metadata = normalizeAgentMessageMetadata(message.metadata);
        const messageStreaming =
          waiting && message.role === "assistant" && message.id === lastMessage?.id;
        const reasoningParts = message.parts.filter((part) => part.type === "reasoning");
        const reasoningText = reasoningParts.map((part) => part.text).join("");
        const firstReasoningIndex = message.parts.findIndex((part) => part.type === "reasoning");
        const hasExplicitReasoningState = reasoningParts.some((part) => part.state !== undefined);
        // AI SDK leaves an open reasoning part marked `streaming` when the
        // transport errors or aborts before reasoning-end. The message status
        // is authoritative for those terminal paths.
        const reasoningStreaming = messageStreaming && (hasExplicitReasoningState
          ? reasoningParts.some((part) => part.state === "streaming")
          : !hasText);

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
                  if (!part.text) return null;
                  return message.role === "assistant" ? (
                    <AgentMarkdown
                      className="chat-text chat-markdown"
                      key={`text-${index}`}
                      text={part.text}
                      streaming={messageStreaming}
                      reducedMotion={Boolean(reducedMotion)}
                    />
                  ) : (
                    <p className="chat-text" key={`text-${index}`}>{part.text}</p>
                  );
                }
                if (part.type === "reasoning" && part.text) {
                  if (index !== firstReasoningIndex) return null;
                  return (
                    <ReasoningDisclosure
                      key={`reasoning-${index}-${reasoningStreaming ? "streaming" : "settled"}`}
                      text={reasoningText}
                      streaming={reasoningStreaming}
                      durationMs={metadata.reasoningMs}
                      reducedMotion={Boolean(reducedMotion)}
                    />
                  );
                }
                if (part.type === "data-agent-tool-result") {
                  const result = normalizeAgentToolResult(part.data);
                  if (!result) return null;
                  // 链接结果由 Agent 面板底部的统一站点卡片区消费；这里再渲染
                  // 通用 ToolCard 会产生重复 DOM，空最终清单还会给出误导文案。
                  if (describeAgentToolResult(result.name, result.result).kind === "links") {
                    return null;
                  }
                  return (
                    <ToolCard
                      key={`tool-${result.toolCallId}-${index}`}
                      storageScope={metadata.conversationId ?? result.toolCallId}
                      result={result}
                      draftState={draftStates[result.toolCallId] ?? IDLE_DRAFT}
                      spaceBatchSuperseded={
                        result.name === "propose_space_batch" &&
                        result.toolCallId !== latestSpaceBatchToolCallId
                      }
                      messagePersisted={metadata.turnPersisted === true}
                      agentWaiting={waiting}
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
              {message.role === "assistant" && !messageStreaming && (
                <ResponseMetrics metadata={metadata} />
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
