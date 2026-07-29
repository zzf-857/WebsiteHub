import {
  AgentContractError,
  agentErrorDetails,
  normalizeAgentConversation,
  normalizeAgentConversationDetail,
  normalizeAgentConversationHistory,
  type AgentConversation,
  type AgentConversationDetail,
  type AgentConversationGroup,
  type AgentConversationHistory,
  type AgentDraftConfirmationKind,
  type AgentSiteDraft,
  type AgentSiteBatchDraft,
  type AgentSiteUpdateDraft,
  type AgentSpaceBatchDraft,
  type AgentSpaceMembershipDraft,
} from "./agent-contract.ts";
import {
  createLibraryCategory,
  createLibrarySite,
  createLibraryTagResolvingConflict,
  listLibraryCategories,
  createLibrarySiteBatch,
  listLibraryTags,
  updateLibrarySite,
} from "./library-client.ts";
import {
  libraryTagNameKey,
  normalizeLibraryTagName,
  type LibrarySite,
  type LibrarySiteUpdateInput,
  type LibraryTag,
} from "./library-contract.ts";
import { addSpaceMember, addSpaceMembersBatch, removeSpaceMember } from "./space-client.ts";
import type { SpaceMemberBatchResult } from "./space-contract.ts";

const CONVERSATION_BASE = "/api/backend/conversations";
export const AGENT_CHAT_API = "/api/backend/agent/chat";
export const DEFAULT_CONVERSATION_PAGE_SIZE = 50;

export class AgentApiError extends Error {
  status: number;
  code?: string;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = "AgentApiError";
    this.status = status;
    if (code) this.code = code;
  }
}

async function readJson(response: Response): Promise<unknown> {
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) return null;
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function request(path: string, init: RequestInit = {}): Promise<unknown> {
  const response = await fetch(`${CONVERSATION_BASE}${path}`, {
    ...init,
    cache: "no-store",
    credentials: "include",
    headers: init.body ? { "Content-Type": "application/json", ...init.headers } : init.headers,
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const error = agentErrorDetails(response.status, payload);
    throw new AgentApiError(response.status, error.message, error.code);
  }
  return payload;
}

function encodeId(id: string): string {
  const normalized = id.trim();
  if (!normalized) throw new TypeError("会话 ID 不能为空");
  return encodeURIComponent(normalized);
}

/**
 * The backend groups history by the *viewer's* day boundaries, so the browser
 * timezone has to travel with the request or "今天" would be wrong abroad.
 */
export function conversationTimezoneOffsetMinutes(now: Date = new Date()): number {
  const offset = -now.getTimezoneOffset();
  return Math.min(840, Math.max(-840, Math.trunc(offset)));
}

export function buildAgentConversationLink(currentHref: string, conversationId: string): string {
  const current = new URL(currentHref);
  const clean = new URL(current.pathname, current.origin);
  clean.searchParams.set("c", conversationId);
  return clean.toString();
}

export async function listAgentConversations(
  options: { cursor?: string; limit?: number; signal?: AbortSignal } = {},
): Promise<AgentConversationHistory> {
  const params = new URLSearchParams();
  if (options.cursor?.trim()) params.set("cursor", options.cursor.trim());
  params.set("limit", String(Math.min(100, Math.max(1, options.limit ?? DEFAULT_CONVERSATION_PAGE_SIZE))));
  params.set("timezone_offset_minutes", String(conversationTimezoneOffsetMinutes()));
  return normalizeAgentConversationHistory(
    await request(`?${params.toString()}`, { signal: options.signal }),
  );
}

/** Load every history page so older conversations remain reachable. */
export async function listAllAgentConversations(
  options: { limit?: number; signal?: AbortSignal } = {},
): Promise<AgentConversationHistory> {
  const limit = Math.min(100, Math.max(1, options.limit ?? 100));
  const groups: AgentConversationGroup[] = [];
  const groupsByKey = new Map<string, AgentConversationGroup>();
  const seenConversationIds = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | undefined;
  let totalCount = 0;

  do {
    const page = await listAgentConversations({ cursor, limit, signal: options.signal });
    totalCount = page.totalCount;
    for (const pageGroup of page.groups) {
      let group = groupsByKey.get(pageGroup.key);
      if (!group) {
        group = { key: pageGroup.key, label: pageGroup.label, items: [] };
        groupsByKey.set(group.key, group);
        groups.push(group);
      }
      for (const conversation of pageGroup.items) {
        if (seenConversationIds.has(conversation.id)) continue;
        seenConversationIds.add(conversation.id);
        group.items.push(conversation);
      }
    }

    const nextCursor = page.nextCursor ?? undefined;
    if (nextCursor && seenCursors.has(nextCursor)) {
      throw new AgentContractError("会话分页游标重复，无法继续读取历史");
    }
    if (nextCursor) seenCursors.add(nextCursor);
    cursor = nextCursor;
  } while (cursor);

  return { groups, nextCursor: null, totalCount };
}

export async function loadAgentConversation(
  conversationId: string,
  signal?: AbortSignal,
): Promise<AgentConversationDetail> {
  const encodedConversationId = encodeId(conversationId);
  const messages: AgentConversationDetail["messages"] = [];
  const seenMessageIds = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | undefined;
  let conversation: AgentConversation | null = null;

  do {
    const params = new URLSearchParams({ limit: "100" });
    if (cursor) params.set("cursor", cursor);
    const page = normalizeAgentConversationDetail(
      await request(`/${encodedConversationId}?${params.toString()}`, { signal }),
    );
    conversation = page.conversation;
    for (const message of page.messages) {
      if (seenMessageIds.has(message.id)) continue;
      seenMessageIds.add(message.id);
      messages.push(message);
    }

    const nextCursor = page.nextCursor ?? undefined;
    if (nextCursor && seenCursors.has(nextCursor)) {
      throw new AgentContractError("消息分页游标重复，无法继续读取完整会话");
    }
    if (nextCursor) seenCursors.add(nextCursor);
    cursor = nextCursor;
  } while (cursor);

  if (!conversation) {
    throw new AgentContractError("会话详情为空");
  }
  return { conversation, messages, nextCursor: null };
}

export async function renameAgentConversation(
  conversationId: string,
  title: string,
  expectedVersion: number,
): Promise<AgentConversation> {
  return normalizeAgentConversation(
    await request(`/${encodeId(conversationId)}`, {
      method: "PATCH",
      body: JSON.stringify({ title: title.trim(), expected_version: expectedVersion }),
    }),
  );
}

export async function deleteAgentConversation(
  conversationId: string,
  expectedVersion: number,
): Promise<void> {
  const params = new URLSearchParams({ expected_version: String(expectedVersion) });
  await request(`/${encodeId(conversationId)}?${params.toString()}`, { method: "DELETE" });
}

function findByName<T extends { id: string; name: string }>(
  items: readonly T[],
  name: string,
): T | undefined {
  const normalized = name.trim().toLocaleLowerCase("zh-CN");
  return items.find((item) => item.name.trim().toLocaleLowerCase("zh-CN") === normalized);
}

function normalizedTagNames(names: readonly string[]): string[] {
  const unique = new Map<string, string>();
  for (const rawName of names) {
    const name = normalizeLibraryTagName(rawName);
    if (name) unique.set(libraryTagNameKey(name), name);
  }
  return [...unique.values()];
}

function findTagByName(tags: readonly LibraryTag[], name: string): LibraryTag | undefined {
  const key = libraryTagNameKey(name);
  return tags.find((tag) => libraryTagNameKey(tag.name) === key);
}

/**
 * Turn a confirmed `propose_site` draft into a real Site.
 *
 * The Agent proposes names, not identifiers, so this reuses an existing
 * category/tag when one matches and creates it otherwise.  The write itself
 * goes through the ordinary library endpoints, which means the user's own
 * session — never the Agent — is what authorizes it.
 */
export async function confirmAgentSiteDraft(draft: AgentSiteDraft): Promise<LibrarySite> {
  const categoryName = draft.category.trim();
  let categoryId: string | undefined;
  if (categoryName) {
    const categories = await listLibraryCategories();
    const existing = findByName(categories, categoryName);
    categoryId = (existing ?? (await createLibraryCategory(categoryName))).id;
  }

  const tagIds = await resolveTagIds(draft.tags);

  return createLibrarySite({
    name: draft.name,
    url: draft.url,
    source: "agent",
    ...(draft.description ? { description: draft.description } : {}),
    ...(categoryId ? { categoryId } : {}),
    ...(tagIds.length > 0 ? { tagIds } : {}),
  });
}

export type AgentDraftConfirmationInput =
  | {
      toolCallId: string;
      kind: "site_created" | "site_updated";
      siteId: string;
    }
  | {
      toolCallId: string;
      kind: "site_batch_created" | "reclassify_applied";
    }
  | {
      toolCallId: string;
      kind: "space_member_added" | "space_member_removed";
      siteId: string;
      spaceId: string;
    }
  | {
      toolCallId: string;
      kind: "space_batch_applied";
      spaceId: string;
      siteIds: string[];
    };

/**
 * Tell the transcript that a draft was confirmed.
 *
 * The write itself already happened through the library/spaces endpoints; this
 * only records the fact, because history replay reads message text and would
 * otherwise keep showing the draft as still pending — which is how the Agent
 * ended up insisting a site "was never saved" right after saving it.
 *
 * Deliberately sends identifiers and a bounded operation kind only. The server
 * composes the recorded sentence and owns the confirmation metadata.
 *
 * The caller keeps business success separate from marker sync failure, so a
 * retry can repair history without repeating the already completed write.
 */
export async function recordAgentDraftConfirmation(
  conversationId: string,
  input: AgentDraftConfirmationInput,
  signal?: AbortSignal,
): Promise<void> {
  const kind: AgentDraftConfirmationKind = input.kind;
  await request(`/${encodeId(conversationId)}/draft-confirmations`, {
    method: "POST",
    body: JSON.stringify({
      tool_call_id: input.toolCallId,
      kind,
      ...("siteId" in input ? { site_id: input.siteId } : {}),
      ...("spaceId" in input ? { space_id: input.spaceId } : {}),
      ...("siteIds" in input ? { site_ids: input.siteIds } : {}),
    }),
    signal,
  });
}

/** 把标签名按服务端规则解析成 id；缺失时创建，并恢复并发同名冲突。 */
async function resolveTagIds(names: readonly string[]): Promise<string[]> {
  const wanted = normalizedTagNames(names);
  if (wanted.length === 0) return [];
  let knownTags = await listLibraryTags();
  const ids: string[] = [];
  for (const name of wanted) {
    const found = findTagByName(knownTags, name);
    if (found) {
      ids.push(found.id);
      continue;
    }
    const result = await createLibraryTagResolvingConflict(name);
    ids.push(result.tag.id);
    knownTags = result.latestTags ?? [...knownTags, result.tag];
  }
  return ids;
}

/**
 * Apply a confirmed `propose_site_update` draft.
 *
 * Only the fields the draft actually proposes are sent, so confirming a rename
 * cannot quietly reset a description or a category the user never mentioned.
 * `expectedVersion` comes from the moment the draft was generated: if the site
 * changed in between, the backend answers 409 and this throws rather than
 * clobbering that change.
 */
export async function confirmAgentSiteUpdate(draft: AgentSiteUpdateDraft): Promise<LibrarySite> {
  const { changes } = draft;
  const input: LibrarySiteUpdateInput = { expectedVersion: draft.expectedVersion };

  if (changes.name !== undefined) input.name = changes.name;
  // 空字符串在这里的语义是「清空说明」，对应后端的 null。
  if (changes.description !== undefined) input.description = changes.description || null;
  if (changes.pinned !== undefined) input.pinned = changes.pinned;
  if (changes.category !== undefined) {
    const categories = await listLibraryCategories();
    const existing = findByName(categories, changes.category);
    input.categoryId = (existing ?? (await createLibraryCategory(changes.category))).id;
  }
  if (changes.tags !== undefined) input.tagIds = await resolveTagIds(changes.tags);

  return updateLibrarySite(draft.siteId, input);
}

export type AgentBatchConfirmResult = { created: number; duplicate: number; failed: number };

/**
 * Apply a confirmed `propose_sites` draft.
 *
 * The URL list travels back to the server, which re-checks each one and
 * creates them item by item — one failure cannot take the rest down.
 */
export async function confirmAgentSiteBatch(
  draft: AgentSiteBatchDraft,
): Promise<AgentBatchConfirmResult> {
  const result = await createLibrarySiteBatch(draft.urls, "agent");
  return { created: result.created, duplicate: result.duplicate, failed: result.failed };
}

/**
 * Apply a confirmed `propose_space_membership` draft.
 *
 * The write goes through the ordinary Space endpoints, authorised by the user's
 * own session — the Agent only ever produced the proposal.
 */
export async function confirmAgentSpaceMembership(
  draft: AgentSpaceMembershipDraft,
): Promise<void> {
  if (draft.action === "add") {
    await addSpaceMember(draft.spaceId, {
      expectedVersion: draft.expectedVersion,
      siteId: draft.siteId,
    });
    return;
  }
  await removeSpaceMember(draft.spaceId, draft.siteId, draft.expectedVersion);
}

/**
 * Apply one user-approved Space task. The server creates the target when asked
 * and adds the currently selected candidates as one guarded operation.
 */
export async function confirmAgentSpaceBatch(
  draft: AgentSpaceBatchDraft,
  selectedSiteIds: readonly string[],
  operationId: string,
  signal?: AbortSignal,
): Promise<SpaceMemberBatchResult> {
  const allowed = new Set(draft.sites.map((site) => site.siteId));
  const siteIds = [...new Set(selectedSiteIds)];
  if (siteIds.some((siteId) => !allowed.has(siteId))) {
    throw new Error("Space 任务候选已经变化，请使用最新草稿。");
  }
  if (draft.target.mode === "existing" && siteIds.length === 0) {
    throw new Error("至少保留一个要加入 Space 的网站。");
  }
  return addSpaceMembersBatch({
    target: draft.target,
    siteIds,
    operationId,
  }, signal);
}
