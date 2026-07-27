import type { UIMessage } from "ai";

/**
 * Wire contract for the Agent chat stream.
 *
 * Two different trust levels live in this file and are handled differently.
 *
 * Stream payloads (`data-agent-*` parts) are shaped by tool output that the
 * model can influence, so every normalizer here is *defensive*: unusable input
 * degrades to a raw view instead of throwing, because a render pass must never
 * crash the conversation.
 *
 * REST payloads (conversation history) come from our own endpoints, so those
 * normalizers throw like the library contract does — a shape mismatch there is
 * a bug worth surfacing.
 */

export const AGENT_SOURCE_LIBRARY = "站内存储数据";
export const AGENT_SOURCE_WEB = "联网搜索";
export const AGENT_SOURCE_MODEL = "llm推荐";

export const MAX_AGENT_MESSAGE_LENGTH = 64_000;

export type AgentToolName =
  | "search_library"
  | "get_site_detail"
  | "list_categories"
  | "list_tags"
  | "list_spaces"
  | "web_search"
  | "propose_site"
  | "propose_site_update"
  | "propose_sites"
  | "propose_space_membership"
  | "propose_reclassify";

const AGENT_TOOL_LABELS: Record<string, string> = {
  search_library: "检索资料库",
  get_site_detail: "读取网站详情",
  list_categories: "读取分类",
  list_tags: "读取标签",
  list_spaces: "读取 Space",
  web_search: "联网搜索",
  propose_site: "生成收录草稿",
  propose_site_update: "生成修改草稿",
  propose_sites: "生成批量收录草稿",
  propose_space_membership: "生成 Space 变更草稿",
  propose_reclassify: "生成全库重分类草稿",
};


export function agentToolLabel(name: string): string {
  return AGENT_TOOL_LABELS[name] ?? name;
}

export type AgentToolCall = {
  toolCallId: string;
  name: string;
  arguments: Record<string, unknown>;
};

export type AgentToolResult = {
  toolCallId: string;
  name: string;
  result: unknown;
};

export type AgentStreamError = {
  code: string;
  message: string;
};

export type AgentMessageMetadata = {
  conversationId?: string;
  provider?: string;
  model?: string;
  webSearch?: boolean;
  errorCode?: string;
};

export type AgentDataParts = {
  "agent-tool-call": AgentToolCall;
  "agent-tool-result": AgentToolResult;
  "agent-error": AgentStreamError;
};

export type AgentUIMessage = UIMessage<AgentMessageMetadata, AgentDataParts>;

type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown): JsonRecord | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  return value as JsonRecord;
}

function asTrimmed(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function asCount(value: unknown): number | null {
  return Number.isSafeInteger(value) && (value as number) >= 0 ? (value as number) : null;
}

function asPositiveCount(value: unknown): number | null {
  return Number.isSafeInteger(value) && (value as number) > 0 ? (value as number) : null;
}

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(asTrimmed).filter((entry): entry is string => entry !== null);
}

/** Only http(s) survives: tool output ends up in an `href`. */
function asWebUrl(value: unknown): string | null {
  const candidate = asTrimmed(value);
  if (candidate === null) return null;
  try {
    const parsed = new URL(candidate);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? candidate : null;
  } catch {
    return null;
  }
}

export function normalizeAgentToolCall(value: unknown): AgentToolCall | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const name = asTrimmed(candidate.name);
  if (name === null) return null;
  return {
    toolCallId: asTrimmed(candidate.toolCallId) ?? name,
    name,
    arguments: asRecord(candidate.arguments) ?? {},
  };
}

export function normalizeAgentToolResult(value: unknown): AgentToolResult | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const name = asTrimmed(candidate.name);
  if (name === null) return null;
  return {
    toolCallId: asTrimmed(candidate.toolCallId) ?? name,
    name,
    result: candidate.result,
  };
}

export function normalizeAgentStreamError(value: unknown): AgentStreamError | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const message = asTrimmed(candidate.message);
  if (message === null) return null;
  return { code: asTrimmed(candidate.code) ?? "agent_error", message };
}

export function normalizeAgentMessageMetadata(value: unknown): AgentMessageMetadata {
  const candidate = asRecord(value);
  if (candidate === null) return {};
  const conversationId = asTrimmed(candidate.conversationId);
  const provider = asTrimmed(candidate.provider);
  const model = asTrimmed(candidate.model);
  const errorCode = asTrimmed(candidate.errorCode);
  return {
    ...(conversationId ? { conversationId } : {}),
    ...(provider ? { provider } : {}),
    ...(model ? { model } : {}),
    ...(typeof candidate.webSearch === "boolean" ? { webSearch: candidate.webSearch } : {}),
    ...(errorCode ? { errorCode } : {}),
  };
}

export type AgentToolLink = {
  siteId: string | null;
  name: string;
  url: string | null;
  description: string | null;
  category: string | null;
  tags: string[];
  pinned: boolean;
};

export type AgentSiteDraft = {
  url: string;
  name: string;
  description: string;
  category: string;
  tags: string[];
};

/** 只包含用户明确要改的字段；缺席 = 保持原样。 */
export type AgentSiteUpdateChanges = {
  name?: string;
  description?: string;
  category?: string;
  tags?: string[];
  pinned?: boolean;
};

export type AgentSiteUpdateDraft = {
  siteId: string;
  // 草稿生成那一刻的行版本。确认时原样回传，中途被别处改过就报冲突而不是覆盖。
  expectedVersion: number;
  before: AgentToolLink;
  changes: AgentSiteUpdateChanges;
  after: AgentToolLink;
};

export type AgentBatchItem = {
  url: string;
  status: "ready" | "duplicate" | "invalid";
  reason: string | null;
};

export type AgentSiteBatchDraft = {
  urls: string[];
  total: number;
  ready: number;
  duplicate: number;
  invalid: number;
  items: AgentBatchItem[];
};

export type AgentSpaceMembershipDraft = {
  action: "add" | "remove";
  siteId: string;
  siteName: string;
  spaceId: string;
  spaceName: string;
  expectedVersion: number;
};

export type AgentReclassifyDraft = {
  siteCount: number;
  estimatedRequestCount: number;
  maximumRequestCount: number;
  estimatedInputCharacters: number;
  allowedCategories: string[];
  expectedCategories: Record<string, string>;
  expectedVersions: Record<string, number>;
};

/** 确认按钮回传给面板的东西：四类草稿共用一个入口，避免四套并行的状态管道。 */
export type AgentDraftAction =
  | { kind: "site"; draft: AgentSiteDraft }
  | { kind: "site_update"; draft: AgentSiteUpdateDraft }
  | { kind: "site_batch"; draft: AgentSiteBatchDraft }
  | { kind: "space_membership"; draft: AgentSpaceMembershipDraft }
  | { kind: "reclassify"; draft: AgentReclassifyDraft };

export type AgentToolFacet = {
  id: string;
  name: string;
  count: number | null;
};

/** A render-ready projection of one tool result. */
export type AgentToolView =
  | { kind: "links"; source: string | null; items: AgentToolLink[]; matchedCount: number | null }
  | { kind: "facets"; source: string | null; items: AgentToolFacet[] }
  | { kind: "draft"; draft: AgentSiteDraft; duplicate: AgentToolLink | null }
  | { kind: "site-update"; draft: AgentSiteUpdateDraft }
  | { kind: "site-batch"; draft: AgentSiteBatchDraft }
  | { kind: "space-membership"; draft: AgentSpaceMembershipDraft }
  | { kind: "reclassify"; draft: AgentReclassifyDraft }
  | { kind: "noop"; message: string }
  | { kind: "rejected"; reason: string }
  | { kind: "error"; source: string | null; message: string }
  | { kind: "raw"; text: string };


function toLink(value: unknown): AgentToolLink | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const url = asWebUrl(candidate.url);
  const name = asTrimmed(candidate.name) ?? asTrimmed(candidate.title) ?? url;
  if (name === null) return null;
  return {
    siteId: asTrimmed(candidate.site_id),
    name,
    url,
    description: asTrimmed(candidate.description) ?? asTrimmed(candidate.snippet),
    category: asTrimmed(candidate.category),
    tags: asStringList(candidate.tags),
    pinned: candidate.pinned === true,
  };
}

function toFacet(value: unknown): AgentToolFacet | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const name = asTrimmed(candidate.name);
  if (name === null) return null;
  return {
    id: asTrimmed(candidate.id) ?? name,
    name,
    count: asCount(candidate.site_count) ?? asCount(candidate.member_count),
  };
}

function toDraft(value: unknown): AgentSiteDraft | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const url = asWebUrl(candidate.url);
  const name = asTrimmed(candidate.name);
  if (url === null || name === null) return null;
  return {
    url,
    name,
    description: asTrimmed(candidate.description) ?? "",
    category: asTrimmed(candidate.category) ?? "",
    tags: asStringList(candidate.tags),
  };
}

function asVersion(value: unknown): number | null {
  return Number.isSafeInteger(value) && (value as number) >= 1 ? (value as number) : null;
}

// before/after 复用 AgentToolLink 的形状（工具返回的就是 _site_summary），
// 但这里的条目没有 url 时也要能渲染，所以缺 url 只让它变成 null，不整体判废。
function toSiteFields(value: unknown): AgentToolLink | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const name = asTrimmed(candidate.name);
  if (name === null) return null;
  return {
    siteId: asTrimmed(candidate.site_id),
    name,
    url: asWebUrl(candidate.url),
    description: asTrimmed(candidate.description),
    category: asTrimmed(candidate.category),
    tags: asStringList(candidate.tags),
    pinned: candidate.pinned === true,
  };
}

// 只挑出后端真正给了的键。用 `in` 判断而不是真值判断，
// 否则 description: "" 与 pinned: false 这两个合法的改动会被当成没改。
function toSiteUpdateChanges(value: unknown): AgentSiteUpdateChanges | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const changes: AgentSiteUpdateChanges = {};
  if (typeof candidate.name === "string") changes.name = candidate.name;
  if (typeof candidate.description === "string") changes.description = candidate.description;
  if (typeof candidate.category === "string") changes.category = candidate.category;
  if (Array.isArray(candidate.tags)) changes.tags = asStringList(candidate.tags);
  if (typeof candidate.pinned === "boolean") changes.pinned = candidate.pinned;
  return Object.keys(changes).length > 0 ? changes : null;
}

function toSiteUpdateDraft(value: unknown): AgentSiteUpdateDraft | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const siteId = asTrimmed(candidate.site_id);
  const expectedVersion = asVersion(candidate.expected_version);
  const before = toSiteFields(candidate.before);
  const after = toSiteFields(candidate.after);
  const changes = toSiteUpdateChanges(candidate.changes);
  if (siteId === null || expectedVersion === null || before === null || after === null) return null;
  if (changes === null) return null;
  return { siteId, expectedVersion, before, changes, after };
}

function toBatchItems(value: unknown): AgentBatchItem[] {
  if (!Array.isArray(value)) return [];
  const items: AgentBatchItem[] = [];
  for (const entry of value) {
    const candidate = asRecord(entry);
    if (candidate === null) continue;
    const url = asTrimmed(candidate.url);
    const status = candidate.status;
    if (url === null) continue;
    if (status !== "ready" && status !== "duplicate" && status !== "invalid") continue;
    items.push({ url, status, reason: asTrimmed(candidate.reason) });
  }
  return items;
}

function toSiteBatchDraft(value: unknown): AgentSiteBatchDraft | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const urls = asStringList(candidate.urls);
  const items = toBatchItems(candidate.items);
  // 一张确认卡如果一条都不会写，就不该出现——它点了等于没点。
  if (urls.length === 0) return null;
  return {
    urls,
    total: asCount(candidate.total) ?? items.length,
    ready: asCount(candidate.ready) ?? urls.length,
    duplicate: asCount(candidate.duplicate) ?? 0,
    invalid: asCount(candidate.invalid) ?? 0,
    items,
  };
}

function toSpaceMembershipDraft(value: unknown): AgentSpaceMembershipDraft | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const action = candidate.action === "add" || candidate.action === "remove" ? candidate.action : null;
  const siteId = asTrimmed(candidate.site_id);
  const siteName = asTrimmed(candidate.site_name);
  const spaceId = asTrimmed(candidate.space_id);
  const spaceName = asTrimmed(candidate.space_name);
  const expectedVersion = asVersion(candidate.expected_version);
  if (action === null || siteId === null || spaceId === null || expectedVersion === null) return null;
  if (siteName === null || spaceName === null) return null;
  return { action, siteId, siteName, spaceId, spaceName, expectedVersion };
}

function toReclassifyDraft(value: unknown): AgentReclassifyDraft | null {
  const candidate = asRecord(value);
  if (candidate === null) return null;
  const siteCount = asPositiveCount(candidate.site_count);
  const estimatedRequestCount = asPositiveCount(candidate.estimated_request_count);
  const maximumRequestCount = asPositiveCount(candidate.maximum_request_count);
  const estimatedInputCharacters = asCount(candidate.estimated_input_characters);
  if (
    siteCount === null ||
    estimatedRequestCount === null ||
    maximumRequestCount === null ||
    maximumRequestCount < estimatedRequestCount ||
    estimatedInputCharacters === null
  ) return null;
  const allowedCategories = asStringList(candidate.allowed_categories);
  const rawCategories = asRecord(candidate.expected_categories);
  const rawVersions = asRecord(candidate.expected_versions);
  if (rawCategories === null || rawVersions === null) return null;
  const expectedCategories: Record<string, string> = {};
  const expectedVersions: Record<string, number> = {};
  for (const [key, value] of Object.entries(rawCategories)) {
    const categoryId = asTrimmed(key);
    const categoryName = asTrimmed(value);
    if (categoryId === null || categoryName === null || categoryId !== key) return null;
    expectedCategories[categoryId] = categoryName;
  }
  for (const [key, value] of Object.entries(rawVersions)) {
    const siteId = asTrimmed(key);
    const version = asVersion(value);
    if (siteId === null || version === null || siteId !== key) return null;
    expectedVersions[siteId] = version;
  }
  if (Object.keys(expectedCategories).length !== allowedCategories.length) return null;
  if (Object.keys(expectedVersions).length !== siteCount) return null;
  const expectedCategoryNames = Object.values(expectedCategories);
  if (
    new Set(expectedCategoryNames).size !== expectedCategoryNames.length ||
    new Set(allowedCategories).size !== allowedCategories.length ||
    expectedCategoryNames.some((name) => !allowedCategories.includes(name))
  ) return null;
  return {
    siteCount,
    estimatedRequestCount,
    maximumRequestCount,
    estimatedInputCharacters,
    allowedCategories,
    expectedCategories,
    expectedVersions,
  };
}

const FACET_TOOLS = new Set(["list_categories", "list_tags", "list_spaces"]);

/** 导入任务的后端状态 → 中文标签。未知状态原样显示，不猜。 */
const IMPORT_STATE_LABELS: Record<string, string> = {
  receiving: "上传中",
  queued_parse: "等待解析",
  parsing: "解析中",
  parse_preview_ready: "预览就绪",
  failed: "解析失败",
};

/**
 * Project one tool result into something the thread can render.
 *
 * The fallback is deliberate: an unrecognised payload becomes truncated raw
 * text rather than disappearing, so a backend change is visible instead of
 * silently dropping provenance.
 */
export function describeAgentToolResult(name: string, result: unknown): AgentToolView {
  const payload = asRecord(result);
  if (payload === null) {
    const text = typeof result === "string" ? result.trim() : JSON.stringify(result ?? null);
    return { kind: "raw", text: (text ?? "").slice(0, 600) };
  }

  const source = asTrimmed(payload.source);
  const error = asTrimmed(payload.error);
  if (error !== null) return { kind: "error", source, message: error };

  const status = asTrimmed(payload.status);

  if (name === "propose_reclassify") {
    if (status === "rejected") {
      return { kind: "rejected", reason: asTrimmed(payload.reason) ?? "无法生成重分类提案" };
    }
    if (status === "noop") {
      return { kind: "noop", message: asTrimmed(payload.message) ?? "资料库中没有需要分类的网站。" };
    }
    const draft = toReclassifyDraft(payload.draft);
    if (draft !== null) return { kind: "reclassify", draft };
  }


  if (name === "propose_site") {
    if (status === "rejected") {
      return { kind: "rejected", reason: asTrimmed(payload.reason) ?? "该网址无法收录" };
    }
    const draft = toDraft(payload.draft);
    if (draft !== null) {
      return { kind: "draft", draft, duplicate: toLink(payload.duplicate) };
    }
  }

  if (name === "propose_sites") {
    if (status === "rejected") {
      return { kind: "rejected", reason: asTrimmed(payload.reason) ?? "没有可收录的网址" };
    }
    if (status === "noop") {
      return { kind: "noop", message: asTrimmed(payload.message) ?? "没有需要新增的网址。" };
    }
    const draft = toSiteBatchDraft(payload.draft);
    if (draft !== null) return { kind: "site-batch", draft };
  }

  if (name === "propose_site_update" || name === "propose_space_membership") {
    if (status === "rejected") {
      const reason = asTrimmed(payload.reason) ?? "无法生成修改草稿";
      const available = asStringList(payload.available_spaces);
      // 目标 Space 不存在时把现有 Space 列出来，比只说一句「找不到」有用。
      return {
        kind: "rejected",
        reason: available.length > 0 ? `${reason}已有的 Space：${available.join("、")}。` : reason,
      };
    }
    if (status === "noop") {
      return { kind: "noop", message: asTrimmed(payload.message) ?? "当前已经是这个状态，无需修改。" };
    }
    const draft =
      name === "propose_site_update"
        ? toSiteUpdateDraft(payload.draft)
        : toSpaceMembershipDraft(payload.draft);
    if (draft !== null) {
      return name === "propose_site_update"
        ? { kind: "site-update", draft: draft as AgentSiteUpdateDraft }
        : { kind: "space-membership", draft: draft as AgentSpaceMembershipDraft };
    }
  }

  if (FACET_TOOLS.has(name) && Array.isArray(payload.items)) {
    return {
      kind: "facets",
      source,
      items: payload.items.map(toFacet).filter((item): item is AgentToolFacet => item !== null),
    };
  }

  // 导入任务行没有 name/url（只有 job_id / state / created_at），走不了 toLink：
  // 每条都会被 filter 掉，结果渲染成「没有命中任何结果」——明明有任务在跑。
  // 投影成 facets，用状态中文名当标签。
  if (name === "list_bookmark_imports" && Array.isArray(payload.items)) {
    const items = payload.items
      .map((entry): AgentToolFacet | null => {
        const row = asRecord(entry);
        const jobId = row === null ? null : asTrimmed(row.job_id);
        if (jobId === null) return null;
        const state = (row !== null && asTrimmed(row.state)) || "未知状态";
        return { id: jobId, name: IMPORT_STATE_LABELS[state] ?? state, count: null };
      })
      .filter((item): item is AgentToolFacet => item !== null);
    return { kind: "facets", source, items };
  }

  if (Array.isArray(payload.items)) {
    return {
      kind: "links",
      source,
      items: payload.items.map(toLink).filter((item): item is AgentToolLink => item !== null),
      matchedCount: asCount(payload.matched_count),
    };
  }

  const single = toLink(payload);
  if (single !== null) {
    return { kind: "links", source, items: [single], matchedCount: null };
  }

  return { kind: "raw", text: JSON.stringify(payload).slice(0, 600) };
}

/**
 * The provenance labels the todolist requires on every answer.
 *
 * With no tool results at all the answer came from the model, which must be
 * said out loud rather than left ambiguous.
 */
export function agentSourceLabels(results: readonly AgentToolResult[]): string[] {
  const labels: string[] = [];
  for (const entry of results) {
    const view = describeAgentToolResult(entry.name, entry.result);
    const source = "source" in view ? view.source : null;
    if (source && !labels.includes(source)) labels.push(source);
  }
  if (labels.length === 0) labels.push(AGENT_SOURCE_MODEL);
  return labels;
}

export type AgentChatRequestInput = {
  message: string;
  conversationId?: string | null;
  metadata?: Record<string, unknown>;
};

type TextualMessage = { role: string; parts?: readonly unknown[] };

/**
 * The text of the newest user turn.
 *
 * `useChat` hands the transport the whole client-side transcript, but the
 * backend only wants the new question — it replays history from its own tables.
 */
export function latestAgentUserText(messages: readonly TextualMessage[]): string {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role !== "user") continue;
    const text = (message.parts ?? [])
      .map((part) => {
        const candidate = asRecord(part);
        return candidate?.type === "text" && typeof candidate.text === "string" ? candidate.text : "";
      })
      .join("");
    if (text.trim()) return text.trim();
  }
  return "";
}

export class AgentContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AgentContractError";
  }
}

/**
 * Build the POST body for `/agent/chat`.
 *
 * The backend takes a single message plus an optional conversation id and
 * replays history from its own tables, so the client never uploads a
 * transcript — that keeps one account's history out of another's request.
 */
export function prepareAgentChatRequest(input: AgentChatRequestInput): JsonRecord {
  const message = typeof input.message === "string" ? input.message.trim() : "";
  if (!message) throw new AgentContractError("消息内容不能为空");
  if (Array.from(message).length > MAX_AGENT_MESSAGE_LENGTH) {
    throw new AgentContractError(`消息不能超过 ${MAX_AGENT_MESSAGE_LENGTH} 个字符`);
  }
  const conversationId = asTrimmed(input.conversationId);
  return {
    message,
    ...(conversationId ? { conversation_id: conversationId } : {}),
    ...(input.metadata && Object.keys(input.metadata).length > 0
      ? { metadata: input.metadata }
      : {}),
  };
}

export type AgentConversation = {
  id: string;
  title: string;
  titleIsCustom: boolean;
  version: number;
  messageCount: number;
  lastMessageAt: string;
};

export type AgentConversationGroup = {
  key: string;
  label: string;
  items: AgentConversation[];
};

export type AgentConversationHistory = {
  groups: AgentConversationGroup[];
  nextCursor: string | null;
  totalCount: number;
};

export type AgentStoredMessage = {
  id: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  sources: AgentToolResult[];
  status: "streaming" | "complete" | "error" | "aborted";
};

function requiredRecord(value: unknown, path: string): JsonRecord {
  const candidate = asRecord(value);
  if (candidate === null) throw new AgentContractError(`${path} 必须是对象`);
  return candidate;
}

function requiredText(value: unknown, path: string): string {
  const candidate = asTrimmed(value);
  if (candidate === null) throw new AgentContractError(`${path} 必须是非空字符串`);
  return candidate;
}

function requiredCount(value: unknown, path: string): number {
  const candidate = asCount(value);
  if (candidate === null) throw new AgentContractError(`${path} 必须是非负整数`);
  return candidate;
}

export function normalizeAgentConversation(value: unknown): AgentConversation {
  const candidate = requiredRecord(value, "conversation");
  return {
    id: requiredText(candidate.id, "conversation.id"),
    title: requiredText(candidate.title, "conversation.title"),
    titleIsCustom: candidate.title_is_custom === true,
    version: requiredCount(candidate.version, "conversation.version"),
    messageCount: requiredCount(candidate.message_count, "conversation.message_count"),
    lastMessageAt: requiredText(candidate.last_message_at, "conversation.last_message_at"),
  };
}

export function normalizeAgentConversationHistory(value: unknown): AgentConversationHistory {
  const candidate = requiredRecord(value, "history");
  if (!Array.isArray(candidate.groups)) {
    throw new AgentContractError("history.groups 必须是数组");
  }
  const nextCursor = asTrimmed(candidate.next_cursor);
  return {
    groups: candidate.groups.map((group, index) => {
      const entry = requiredRecord(group, `history.groups[${index}]`);
      if (!Array.isArray(entry.items)) {
        throw new AgentContractError(`history.groups[${index}].items 必须是数组`);
      }
      return {
        key: requiredText(entry.key, `history.groups[${index}].key`),
        label: requiredText(entry.label, `history.groups[${index}].label`),
        items: entry.items.map(normalizeAgentConversation),
      };
    }),
    nextCursor,
    totalCount: requiredCount(candidate.total_count, "history.total_count"),
  };
}

const MESSAGE_ROLES = new Set(["system", "user", "assistant", "tool"]);
const MESSAGE_STATUSES = new Set(["streaming", "complete", "error", "aborted"]);

export function normalizeAgentStoredMessage(value: unknown): AgentStoredMessage {
  const candidate = requiredRecord(value, "message");
  const role = requiredText(candidate.role, "message.role");
  if (!MESSAGE_ROLES.has(role)) throw new AgentContractError("message.role 不是受支持的值");
  const status = requiredText(candidate.status, "message.status");
  if (!MESSAGE_STATUSES.has(status)) throw new AgentContractError("message.status 不是受支持的值");
  const sources = Array.isArray(candidate.sources)
    ? candidate.sources
        .map(normalizeAgentToolResult)
        .filter((entry): entry is AgentToolResult => entry !== null)
    : [];
  return {
    id: requiredText(candidate.id, "message.id"),
    role: role as AgentStoredMessage["role"],
    content: typeof candidate.content === "string" ? candidate.content : "",
    sources,
    status: status as AgentStoredMessage["status"],
  };
}

export type AgentConversationDetail = {
  conversation: AgentConversation;
  messages: AgentStoredMessage[];
};

export function normalizeAgentConversationDetail(value: unknown): AgentConversationDetail {
  const candidate = requiredRecord(value, "detail");
  if (!Array.isArray(candidate.messages)) {
    throw new AgentContractError("detail.messages 必须是数组");
  }
  return {
    conversation: normalizeAgentConversation(candidate.conversation),
    messages: candidate.messages.map(normalizeAgentStoredMessage),
  };
}

/**
 * Rebuild UI messages from the archive so a reopened conversation looks like
 * the live stream did: tool provenance first, then the answer text.
 */
export function toAgentUIMessages(messages: readonly AgentStoredMessage[]): AgentUIMessage[] {
  const restored: AgentUIMessage[] = [];
  for (const message of messages) {
    if (message.role !== "user" && message.role !== "assistant") continue;
    if (!message.content && message.sources.length === 0) continue;
    const parts: AgentUIMessage["parts"] = message.sources.map((source) => ({
      type: "data-agent-tool-result",
      data: source,
    }));
    if (message.content) parts.push({ type: "text", text: message.content });
    restored.push({ id: message.id, role: message.role, parts });
  }
  return restored;
}

export type AgentErrorDetails = {
  code?: string;
  message: string;
};

function fallbackAgentErrorMessage(status: number): string {
  if (status === 401) return "登录状态已失效，请重新登录";
  if (status === 404) return "会话不存在或已被删除";
  if (status === 409) return "会话已被更新，请刷新后重试";
  if (status === 422) return "提交的信息不符合要求";
  if (status === 429) return "操作过于频繁，请稍后重试";
  return "Agent 服务暂时不可用，请稍后重试";
}

export function agentErrorDetails(status: number, payload: unknown): AgentErrorDetails {
  const candidate = asRecord(payload);
  let code: string | undefined;
  let message: string | undefined;

  if (candidate !== null) {
    code = asTrimmed(candidate.code) ?? undefined;
    message = asTrimmed(candidate.message) ?? undefined;
    const detail = asRecord(candidate.detail);
    if (detail !== null) {
      code = asTrimmed(detail.code) ?? code;
      message = asTrimmed(detail.message) ?? message;
    } else if (typeof candidate.detail === "string") {
      message = asTrimmed(candidate.detail) ?? message;
    } else if (Array.isArray(candidate.detail)) {
      const first = candidate.detail.map(asRecord).find((entry) => entry !== null);
      message = asTrimmed(first?.msg) ?? message;
    }
  }

  return { ...(code ? { code } : {}), message: message ?? fallbackAgentErrorMessage(status) };
}
