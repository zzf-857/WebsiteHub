import {
  agentErrorDetails,
  normalizeAgentConversation,
  normalizeAgentConversationDetail,
  normalizeAgentConversationHistory,
  type AgentConversation,
  type AgentConversationDetail,
  type AgentConversationHistory,
  type AgentSiteDraft,
  type AgentSiteUpdateDraft,
  type AgentSpaceMembershipDraft,
} from "./agent-contract.ts";
import {
  createLibraryCategory,
  createLibrarySite,
  createLibraryTag,
  listLibraryCategories,
  listLibraryTags,
  updateLibrarySite,
} from "./library-client.ts";
import type { LibrarySite, LibrarySiteUpdateInput } from "./library-contract.ts";
import { addSpaceMember, removeSpaceMember } from "./space-client.ts";

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

export async function loadAgentConversation(
  conversationId: string,
  signal?: AbortSignal,
): Promise<AgentConversationDetail> {
  return normalizeAgentConversationDetail(
    await request(`/${encodeId(conversationId)}?limit=100`, { signal }),
  );
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

  const wanted = Array.from(
    new Map(
      draft.tags
        .map((tag) => tag.trim())
        .filter(Boolean)
        .map((tag) => [tag.toLocaleLowerCase("zh-CN"), tag]),
    ).values(),
  );
  const tagIds: string[] = [];
  if (wanted.length > 0) {
    const existingTags = await listLibraryTags();
    for (const tag of wanted) {
      const found = findByName(existingTags, tag);
      tagIds.push((found ?? (await createLibraryTag(tag))).id);
    }
  }

  return createLibrarySite({
    name: draft.name,
    url: draft.url,
    ...(draft.description ? { description: draft.description } : {}),
    ...(categoryId ? { categoryId } : {}),
    ...(tagIds.length > 0 ? { tagIds } : {}),
  });
}

/** 把标签名解析成 id，缺失的现建。顺序保持草稿里的顺序。 */
async function resolveTagIds(names: readonly string[]): Promise<string[]> {
  if (names.length === 0) return [];
  const existing = await listLibraryTags();
  const ids: string[] = [];
  for (const name of names) {
    const found = findByName(existing, name);
    ids.push((found ?? (await createLibraryTag(name))).id);
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
