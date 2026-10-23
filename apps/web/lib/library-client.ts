import {
  assertLibraryBulkDeleteItems,
  assertLibraryCategoryName,
  assertLibraryExpectedVersion,
  assertMetadataBackfillRequest,
  assertLibraryRediscoverQuery,
  assertLibrarySiteCreateInput,
  assertLibrarySiteCreateSource,
  assertLibrarySiteKind,
  assertLibrarySiteState,
  assertLibrarySiteStateInput,
  assertLibrarySiteUpdateInput,
  assertLibraryTagName,
  LibraryContractError,
  libraryTagNameKey,
  libraryErrorDetails,
  METADATA_BACKFILL_LIMITS,
  normalizeLibraryAnalysisBackfill,
  normalizeLibraryBulkDeleteResult,
  normalizeCategoryDeletePreview,
  normalizeLibraryCategories,
  normalizeLibraryCategory,
  normalizeLibraryOverview,
  normalizeLibraryRediscover,
  normalizeLibrarySite,
  normalizeLibrarySiteAnalysis,
  normalizeLibrarySiteOpen,
  normalizeLibrarySitePage,
  normalizeLibrarySiteSelection,
  normalizeLibraryTag,
  normalizeLibraryTags,
  normalizeLibraryTagName,
  normalizeMetadataBackfillPlan,
  normalizeMetadataBackfillProgress,
  normalizeSiteSimilarityApply,
  normalizeSiteSimilarityDecision,
  normalizeSiteSimilarityGroupPage,
  normalizeSiteSimilarityRecommendedDecision,
  normalizeSiteSimilarityScan,
  normalizeTagMergePreview,
  normalizeTagMergeResult,
  type LibraryCategory,
  type LibraryAnalysisBackfill,
  type LibraryBulkDeleteItem,
  type LibraryBulkDeleteResult,
  type LibraryCategoryDeletePreview,
  type LibraryOverview,
  type LibraryRediscover,
  type LibraryRediscoverQuery,
  type LibrarySite,
  type LibrarySiteAnalysisResult,
  type LibrarySiteCreateInput,
  type LibrarySiteCreateSource,
  type LibrarySiteOpenResult,
  type LibrarySitePage,
  type LibrarySiteQuery,
  type LibrarySiteSelectionItem,
  type LibrarySiteStateInput,
  type LibrarySiteUpdateInput,
  type LibraryTag,
  type LibraryTagMergePreview,
  type LibraryTagMergeResult,
  type MetadataBackfillPlan,
  type MetadataBackfillProgress,
  type MetadataBackfillRequest,
  type SiteSimilarityApplyResult,
  type SiteSimilarityDecisionResult,
  type SiteSimilarityGroupKind,
  type SiteSimilarityGroupPage,
  type SiteSimilarityRecommendedDecisionResult,
  type SiteSimilarityScan,
} from "./library-contract.ts";
import { isCategoryIconName } from "./category-icons.ts";

const LIBRARY_BASE = "/api/backend/library";
const DISCOVER_BASE = "/api/backend/discover";
export const DEFAULT_LIBRARY_PAGE_SIZE = 24;
export const MAX_LIBRARY_PAGE_SIZE = 100;
export const MAX_LIBRARY_ANALYSIS_BACKFILL = 5_000;
export const DEFAULT_SITE_SIMILARITY_GROUP_PAGE_SIZE = 12;
export const MAX_SITE_SIMILARITY_GROUP_PAGE_SIZE = 24;

function isSiteSimilarityGroupFilter(
  value: unknown,
): value is SiteSimilarityGroupKind | "all" {
  return value === "all" || value === "duplicate" || value === "same_site";
}

export class LibraryApiError extends Error {
  status: number;
  code?: string;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = "LibraryApiError";
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

async function request(
  path: string,
  init: RequestInit = {},
  base: string = LIBRARY_BASE,
): Promise<unknown> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    cache: "no-store",
    credentials: "include",
    headers: init.body
      ? { "Content-Type": "application/json", ...init.headers }
      : init.headers,
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const error = libraryErrorDetails(response.status, payload);
    throw new LibraryApiError(response.status, error.message, error.code);
  }
  return payload;
}

function encodeId(id: string): string {
  const normalized = id.trim();
  if (!normalized) throw new TypeError("资源 ID 不能为空");
  return encodeURIComponent(normalized);
}

function normalizedLimit(limit: number | undefined): number {
  if (limit === undefined) return DEFAULT_LIBRARY_PAGE_SIZE;
  if (!Number.isFinite(limit)) throw new TypeError("limit 必须是有限数字");
  return Math.min(MAX_LIBRARY_PAGE_SIZE, Math.max(1, Math.trunc(limit)));
}

export function buildLibrarySiteSearchParams(query: LibrarySiteQuery = {}): URLSearchParams {
  const params = new URLSearchParams();
  const q = query.q?.trim();
  if (q) params.set("q", q);
  if (query.categoryId?.trim()) params.set("category_id", query.categoryId.trim());
  if (query.tagId?.trim()) params.set("tag_id", query.tagId.trim());
  if (query.spaceId?.trim()) params.set("space_id", query.spaceId.trim());
  if (query.pinned !== undefined) params.set("pinned", String(query.pinned));
  if (query.kind !== undefined) params.set("kind", assertLibrarySiteKind(query.kind));
  if (query.state !== undefined) params.set("state", assertLibrarySiteState(query.state));
  params.set("sort", query.sort ?? "updated");
  params.set("direction", query.direction ?? "desc");
  if (query.cursor?.trim()) params.set("cursor", query.cursor.trim());
  params.set("limit", String(normalizedLimit(query.limit)));
  return params;
}

function siteCreatePayload(input: LibrarySiteCreateInput): Record<string, unknown> {
  const normalized = assertLibrarySiteCreateInput(input);
  return {
    name: normalized.name,
    url: normalized.url,
    ...(normalized.summary !== undefined ? { summary: normalized.summary } : {}),
    ...(normalized.description !== undefined ? { description: normalized.description } : {}),
    ...(normalized.faviconUrl !== undefined ? { favicon_url: normalized.faviconUrl } : {}),
    ...(normalized.categoryId !== undefined ? { category_id: normalized.categoryId } : {}),
    ...(normalized.tagIds !== undefined ? { tag_ids: normalized.tagIds } : {}),
    ...(normalized.pinned !== undefined ? { pinned: normalized.pinned } : {}),
    ...(normalized.source !== undefined ? { source: normalized.source } : {}),
  };
}

function siteUpdatePayload(input: LibrarySiteUpdateInput): Record<string, unknown> {
  const normalized = assertLibrarySiteUpdateInput(input);
  return {
    expected_version: normalized.expectedVersion,
    ...(normalized.name !== undefined ? { name: normalized.name } : {}),
    ...(normalized.url !== undefined ? { url: normalized.url } : {}),
    ...(normalized.summary !== undefined ? { summary: normalized.summary } : {}),
    ...(normalized.description !== undefined ? { description: normalized.description } : {}),
    ...(normalized.faviconUrl !== undefined ? { favicon_url: normalized.faviconUrl } : {}),
    ...(normalized.categoryId !== undefined ? { category_id: normalized.categoryId } : {}),
    ...(normalized.tagIds !== undefined ? { tag_ids: normalized.tagIds } : {}),
    ...(normalized.pinned !== undefined ? { pinned: normalized.pinned } : {}),
    ...(normalized.kind !== undefined ? { kind: normalized.kind } : {}),
  };
}

function siteStatePayload(input: LibrarySiteStateInput): Record<string, unknown> {
  const normalized = assertLibrarySiteStateInput(input);
  return {
    ...(normalized.starred !== undefined ? { starred: normalized.starred } : {}),
    ...(normalized.seen !== undefined ? { seen: normalized.seen } : {}),
    ...(normalized.snoozeDays !== undefined ? { snooze_days: normalized.snoozeDays } : {}),
    ...(normalized.resurfaceMuted !== undefined
      ? { resurface_muted: normalized.resurfaceMuted }
      : {}),
  };
}

export async function listLibraryCategories(signal?: AbortSignal): Promise<LibraryCategory[]> {
  return normalizeLibraryCategories(await request("/categories", { signal }));
}

/** 分类图标只接受白名单里的 lucide 名称；"" 表示恢复按名称自动推断。 */
function categoryIconField(icon: string | undefined): { icon?: string } {
  if (icon === undefined) return {};
  const normalized = icon.trim();
  if (normalized && !isCategoryIconName(normalized)) throw new TypeError("不支持的分类图标");
  return { icon: normalized };
}

export async function createLibraryCategory(name: string, icon?: string): Promise<LibraryCategory> {
  return normalizeLibraryCategory(await request("/categories", {
    method: "POST",
    body: JSON.stringify({ name: assertLibraryCategoryName(name), ...categoryIconField(icon) }),
  }));
}

/** icon 省略 = 不动现有图标（纯重命名）；"" = 恢复自动推断。 */
export async function updateLibraryCategory(
  id: string,
  name: string,
  icon?: string,
): Promise<LibraryCategory> {
  const iconField = categoryIconField(icon);
  return normalizeLibraryCategory(await request(`/categories/${encodeId(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ name: assertLibraryCategoryName(name), ...iconField }),
  }));
}

export async function previewLibraryCategoryDelete(id: string): Promise<LibraryCategoryDeletePreview> {
  return normalizeCategoryDeletePreview(await request(`/categories/${encodeId(id)}/delete-preview`));
}

export async function deleteLibraryCategory(id: string): Promise<void> {
  await request(`/categories/${encodeId(id)}`, { method: "DELETE" });
}

export async function listLibraryTags(signal?: AbortSignal): Promise<LibraryTag[]> {
  return normalizeLibraryTags(await request("/tags", { signal }));
}

export async function createLibraryTag(name: string): Promise<LibraryTag> {
  return normalizeLibraryTag(await request("/tags", {
    method: "POST",
    body: JSON.stringify({ name: assertLibraryTagName(name) }),
  }));
}

export type LibraryTagCreateResolution = {
  tag: LibraryTag;
  /** Present only when a 409 required replacing the caller's stale tag list. */
  latestTags: LibraryTag[] | null;
};

/** Create a tag, or recover a concurrent/normalization-equivalent creation. */
export async function createLibraryTagResolvingConflict(
  name: string,
): Promise<LibraryTagCreateResolution> {
  const display = normalizeLibraryTagName(name);
  const key = libraryTagNameKey(display);
  try {
    return { tag: await createLibraryTag(display), latestTags: null };
  } catch (error) {
    if (!(error instanceof LibraryApiError) || error.status !== 409) throw error;

    const latestTags = await listLibraryTags();
    const existing = latestTags.find((tag) => libraryTagNameKey(tag.name) === key);
    if (!existing) throw error;
    return { tag: existing, latestTags };
  }
}

export async function updateLibraryTag(id: string, name: string): Promise<LibraryTag> {
  return normalizeLibraryTag(await request(`/tags/${encodeId(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ name: assertLibraryTagName(name) }),
  }));
}

function mergePair(sourceId: string, targetId: string): { source: string; target: string } {
  const source = encodeId(sourceId);
  const target = encodeId(targetId);
  if (source === target) throw new TypeError("不能把标签合并到它自己");
  return { source, target };
}

export async function previewLibraryTagMerge(
  sourceId: string,
  targetId: string,
  signal?: AbortSignal,
): Promise<LibraryTagMergePreview> {
  const { source, target } = mergePair(sourceId, targetId);
  return normalizeTagMergePreview(
    await request(`/tags/${source}/merge-preview?target_id=${target}`, { signal }),
  );
}

/** 单事务：源标签的收藏改带目标标签，源标签删除。 */
export async function mergeLibraryTag(sourceId: string, targetId: string): Promise<LibraryTagMergeResult> {
  const { source } = mergePair(sourceId, targetId);
  return normalizeTagMergeResult(await request(`/tags/${source}/merge`, {
    method: "POST",
    body: JSON.stringify({ target_id: targetId.trim() }),
  }));
}

export async function deleteLibraryTag(id: string): Promise<void> {
  await request(`/tags/${encodeId(id)}`, { method: "DELETE" });
}

export async function listLibrarySites(
  query: LibrarySiteQuery = {},
  signal?: AbortSignal,
): Promise<LibrarySitePage> {
  const params = buildLibrarySiteSearchParams(query);
  return normalizeLibrarySitePage(await request(`/sites?${params.toString()}`, { signal }));
}

export async function listLibrarySiteSelection(
  query: LibrarySiteQuery = {},
  signal?: AbortSignal,
): Promise<LibrarySiteSelectionItem[]> {
  const params = buildLibrarySiteSearchParams(query);
  params.delete("sort");
  params.delete("direction");
  params.delete("cursor");
  params.delete("limit");
  return normalizeLibrarySiteSelection(
    await request(`/sites/selection?${params.toString()}`, { signal }),
  );
}

export async function createLibrarySite(input: LibrarySiteCreateInput): Promise<LibrarySite> {
  return normalizeLibrarySite(await request("/sites", {
    method: "POST",
    body: JSON.stringify(siteCreatePayload(input)),
  }));
}

export async function getLibrarySite(id: string, signal?: AbortSignal): Promise<LibrarySite> {
  return normalizeLibrarySite(await request(`/sites/${encodeId(id)}`, { signal }));
}

export async function updateLibrarySite(id: string, input: LibrarySiteUpdateInput): Promise<LibrarySite> {
  return normalizeLibrarySite(await request(`/sites/${encodeId(id)}`, {
    method: "PATCH",
    body: JSON.stringify(siteUpdatePayload(input)),
  }));
}

/**
 * Record one open of a saved site through WebHub. Usage is not an edit: the
 * site's `version` and `updatedAt` stay as they are. `keepalive` lets the
 * count land even when the click also navigates this page away.
 */
export async function recordSiteOpen(
  id: string,
  signal?: AbortSignal,
): Promise<LibrarySiteOpenResult> {
  const siteId = encodeId(id);
  const result = normalizeLibrarySiteOpen(await request(`/sites/${siteId}/open`, {
    method: "POST",
    keepalive: true,
    signal,
  }));
  if (result.siteId !== id.trim()) {
    throw new LibraryContractError("site_open.site_id 与请求的收藏不一致");
  }
  return result;
}

/** Star / mark seen / snooze / mute. Reversible and version-free. */
export async function updateSiteState(
  id: string,
  input: LibrarySiteStateInput,
  signal?: AbortSignal,
): Promise<LibrarySite> {
  const body = JSON.stringify(siteStatePayload(input));
  return normalizeLibrarySite(await request(`/sites/${encodeId(id)}/state`, {
    method: "PUT",
    body,
    signal,
  }));
}

/** Today's resurfacing picks: stable per UTC day; bump `shuffle` for another set. */
export async function fetchRediscover(
  query: LibraryRediscoverQuery = {},
  signal?: AbortSignal,
): Promise<LibraryRediscover> {
  const normalized = assertLibraryRediscoverQuery(query);
  const params = new URLSearchParams({
    limit: String(normalized.limit),
    shuffle: String(normalized.shuffle),
  });
  return normalizeLibraryRediscover(
    await request(`/rediscover?${params.toString()}`, { signal }, DISCOVER_BASE),
  );
}

export async function fetchLibraryOverview(signal?: AbortSignal): Promise<LibraryOverview> {
  return normalizeLibraryOverview(await request("/overview", { signal }));
}

export async function deleteLibrarySite(id: string, expectedVersion: number): Promise<void> {
  const params = new URLSearchParams({
    expected_version: String(assertLibraryExpectedVersion(expectedVersion)),
  });
  await request(`/sites/${encodeId(id)}?${params.toString()}`, { method: "DELETE" });
}

export async function deleteLibrarySites(
  items: LibraryBulkDeleteItem[],
): Promise<LibraryBulkDeleteResult> {
  const normalized = assertLibraryBulkDeleteItems(items);
  return normalizeLibraryBulkDeleteResult(await request("/sites/bulk-delete", {
    method: "POST",
    body: JSON.stringify({
      items: normalized.map((item) => ({
        site_id: item.siteId,
        expected_version: item.expectedVersion,
      })),
    }),
  }));
}

export async function startSiteSimilarityScan(
  signal?: AbortSignal,
): Promise<SiteSimilarityScan> {
  return normalizeSiteSimilarityScan(await request("/site-similarity-scans", {
    method: "POST",
    signal,
  }));
}

export async function getActiveSiteSimilarityScan(
  signal?: AbortSignal,
): Promise<SiteSimilarityScan | null> {
  const payload = await request("/site-similarity-scans/active", { signal });
  return payload === null ? null : normalizeSiteSimilarityScan(payload);
}

export async function listSiteSimilarityGroups(
  runId: string,
  query: {
    kind?: SiteSimilarityGroupKind | "all";
    cursor?: string;
    page?: number;
    limit?: number;
  } = {},
  signal?: AbortSignal,
): Promise<SiteSimilarityGroupPage> {
  const requestedKind = query.kind ?? "all";
  if (!isSiteSimilarityGroupFilter(requestedKind)) {
    throw new TypeError("相似网站分区无效");
  }
  const requestedLimit = query.limit ?? DEFAULT_SITE_SIMILARITY_GROUP_PAGE_SIZE;
  if (!Number.isSafeInteger(requestedLimit) || requestedLimit < 1) {
    throw new TypeError("相似网站分组页大小必须是正整数");
  }
  const requestedPage = query.page ?? 1;
  if (!Number.isSafeInteger(requestedPage) || requestedPage < 1) {
    throw new TypeError("相似网站页码必须是正整数");
  }
  const cursor = query.cursor?.trim();
  if (cursor && query.page !== undefined) {
    throw new TypeError("相似网站页码和游标不能同时使用");
  }
  const params = new URLSearchParams({
    kind: requestedKind,
    limit: String(Math.min(requestedLimit, MAX_SITE_SIMILARITY_GROUP_PAGE_SIZE)),
  });
  if (cursor) params.set("cursor", cursor);
  else params.set("page", String(requestedPage));
  return normalizeSiteSimilarityGroupPage(await request(
    `/site-similarity-scans/${encodeId(runId)}/groups?${params.toString()}`,
    { signal },
  ));
}

export async function selectRecommendedSiteSimilarityDecisions(
  runId: string,
  input: {
    kind: SiteSimilarityGroupKind | "all";
    expectedVersion: number;
  },
  signal?: AbortSignal,
): Promise<SiteSimilarityRecommendedDecisionResult> {
  if (!isSiteSimilarityGroupFilter(input.kind)) {
    throw new TypeError("相似网站分区无效");
  }
  const decision = normalizeSiteSimilarityRecommendedDecision(await request(
    `/site-similarity-scans/${encodeId(runId)}/decisions/recommended`,
    {
      method: "PUT",
      body: JSON.stringify({
        kind: input.kind,
        expected_version: assertLibraryExpectedVersion(input.expectedVersion),
      }),
      signal,
    },
  ));
  if (decision.kind !== input.kind) {
    throw new TypeError("相似网站批量选择响应分区不一致");
  }
  return decision;
}

export async function saveSiteSimilarityDecision(
  runId: string,
  groupId: string,
  input: { keepSiteIds: string[]; expectedVersion: number },
  signal?: AbortSignal,
): Promise<SiteSimilarityDecisionResult> {
  if (!Array.isArray(input.keepSiteIds) || input.keepSiteIds.length > 10_000) {
    throw new TypeError("保留网站列表无效");
  }
  const keepSiteIds = input.keepSiteIds.map((siteId) => siteId.trim());
  if (keepSiteIds.some((siteId) => !siteId)) throw new TypeError("保留网站 ID 不能为空");
  if (new Set(keepSiteIds).size !== keepSiteIds.length) {
    throw new TypeError("保留网站不能重复");
  }
  return normalizeSiteSimilarityDecision(await request(
    `/site-similarity-scans/${encodeId(runId)}/groups/${encodeId(groupId)}/decision`,
    {
      method: "PUT",
      body: JSON.stringify({
        keep_site_ids: keepSiteIds,
        expected_version: assertLibraryExpectedVersion(input.expectedVersion),
      }),
      signal,
    },
  ));
}

export async function applySiteSimilarityScan(
  runId: string,
  expectedVersion: number,
  signal?: AbortSignal,
): Promise<SiteSimilarityApplyResult> {
  return normalizeSiteSimilarityApply(await request(
    `/site-similarity-scans/${encodeId(runId)}/apply`,
    {
      method: "POST",
      body: JSON.stringify({
        expected_version: assertLibraryExpectedVersion(expectedVersion),
      }),
      signal,
    },
  ));
}

/**
 * Fetch public page evidence, run the account's model through the three
 * constrained enrichment tools, then atomically store allowed derived fields.
 */
export async function analyzeLibrarySite(id: string): Promise<LibrarySiteAnalysisResult> {
  return normalizeLibrarySiteAnalysis(await request(`/sites/${encodeId(id)}/analyze`, {
    method: "POST",
  }));
}

export async function backfillLibrarySiteMetadata(
  limit = MAX_LIBRARY_ANALYSIS_BACKFILL,
): Promise<LibraryAnalysisBackfill> {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_LIBRARY_ANALYSIS_BACKFILL) {
    throw new TypeError(`补全批次大小必须是 1 到 ${MAX_LIBRARY_ANALYSIS_BACKFILL} 之间的整数`);
  }
  const params = new URLSearchParams({ limit: String(limit) });
  return normalizeLibraryAnalysisBackfill(await request(`/sites/analyze-missing?${params}`, {
    method: "POST",
  }));
}

/** Preview the immutable target snapshot before spending network or model capacity. */
export async function getMetadataBackfillPlan(
  input: MetadataBackfillRequest,
  signal?: AbortSignal,
): Promise<MetadataBackfillPlan> {
  const normalized = assertMetadataBackfillRequest(input);
  const params = new URLSearchParams({
    mode: normalized.mode,
    limit: String(normalized.limit),
  });
  return normalizeMetadataBackfillPlan(await request(
    `/metadata-backfills/plan?${params.toString()}`,
    { signal },
  ));
}

/** Start (or join) the account's durable metadata backfill job. */
export async function startMetadataBackfill(
  input: MetadataBackfillRequest = {
    mode: "metadata",
    limit: METADATA_BACKFILL_LIMITS.metadata.defaultLimit,
  },
  signal?: AbortSignal,
): Promise<MetadataBackfillProgress> {
  const normalized = assertMetadataBackfillRequest(input);
  return normalizeMetadataBackfillProgress(await request("/metadata-backfills", {
    method: "POST",
    body: JSON.stringify(normalized),
    signal,
  }));
}

/** Read the account's active durable task, if a prior page already started one. */
export async function getActiveMetadataBackfill(
  signal?: AbortSignal,
): Promise<MetadataBackfillProgress | null> {
  const payload = await request("/metadata-backfills/active", { signal });
  return payload === null ? null : normalizeMetadataBackfillProgress(payload);
}

/** Read a previously started metadata backfill job without starting extra work. */
export async function getMetadataBackfillProgress(
  runId: string,
  signal?: AbortSignal,
): Promise<MetadataBackfillProgress> {
  return normalizeMetadataBackfillProgress(await request(
    `/metadata-backfills/${encodeId(runId)}`,
    { signal },
  ));
}

export type LibrarySiteBatchResult = {
  created: number;
  duplicate: number;
  invalid: number;
  failed: number;
};

/** 批量入库。逐项独立提交，一条失败不影响其余。 */
export async function createLibrarySiteBatch(
  urls: string[],
  source: LibrarySiteCreateSource = "manual",
): Promise<LibrarySiteBatchResult> {
  const payload = await request("/sites/batch", {
    method: "POST",
    body: JSON.stringify({
      urls,
      confirm: true,
      source: assertLibrarySiteCreateSource(source),
    }),
  });
  const record = (payload ?? {}) as Record<string, unknown>;
  const count = (key: string): number =>
    Number.isSafeInteger(record[key]) && (record[key] as number) >= 0 ? (record[key] as number) : 0;
  return {
    created: count("created"),
    duplicate: count("duplicate"),
    invalid: count("invalid"),
    failed: count("failed"),
  };
}

/**
 * Move sites within one category.
 *
 * Takes a list plus an anchor rather than an absolute index: an index computed
 * from the list the user was looking at is stale the moment anything else
 * changes, while "put these before that one" stays true. `beforeSiteId: null`
 * means "send them to the end".
 */
export async function reorderLibrarySites(
  categoryId: string,
  input: { orderedSiteIds: string[]; beforeSiteId: string | null },
): Promise<void> {
  await request(`/categories/${encodeId(categoryId)}/reorder`, {
    method: "POST",
    body: JSON.stringify({
      ordered_site_ids: input.orderedSiteIds,
      before_site_id: input.beforeSiteId,
    }),
  });
}

/** 执行 Agent 的全库重分类草稿 */
export async function confirmAgentReclassify(draft: {
  expectedCategories: Record<string, string>;
  expectedVersions: Record<string, number>;
}): Promise<void> {
  await request("/reclassify/apply", {
    method: "POST",
    body: JSON.stringify({
      expected_categories: draft.expectedCategories,
      expected_versions: draft.expectedVersions,
    }),
  });
}
