import {
  assertLibraryBulkDeleteItems,
  assertLibraryCategoryName,
  assertLibraryExpectedVersion,
  assertLibrarySiteCreateInput,
  assertLibrarySiteCreateSource,
  assertLibrarySiteUpdateInput,
  assertLibraryTagName,
  libraryTagNameKey,
  libraryErrorDetails,
  normalizeLibraryAnalysisBackfill,
  normalizeLibraryBulkDeleteResult,
  normalizeCategoryDeletePreview,
  normalizeLibraryCategories,
  normalizeLibraryCategory,
  normalizeLibrarySite,
  normalizeLibrarySiteAnalysis,
  normalizeLibrarySitePage,
  normalizeLibrarySiteSelection,
  normalizeLibraryTag,
  normalizeLibraryTags,
  normalizeLibraryTagName,
  normalizeMetadataBackfillProgress,
  type LibraryCategory,
  type LibraryAnalysisBackfill,
  type LibraryBulkDeleteItem,
  type LibraryBulkDeleteResult,
  type LibraryCategoryDeletePreview,
  type LibrarySite,
  type LibrarySiteAnalysisResult,
  type LibrarySiteCreateInput,
  type LibrarySiteCreateSource,
  type LibrarySitePage,
  type LibrarySiteQuery,
  type LibrarySiteSelectionItem,
  type LibrarySiteUpdateInput,
  type LibraryTag,
  type MetadataBackfillProgress,
} from "./library-contract.ts";

const LIBRARY_BASE = "/api/backend/library";
export const DEFAULT_LIBRARY_PAGE_SIZE = 24;
export const MAX_LIBRARY_PAGE_SIZE = 100;
export const MAX_LIBRARY_ANALYSIS_BACKFILL = 5_000;

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

async function request(path: string, init: RequestInit = {}): Promise<unknown> {
  const response = await fetch(`${LIBRARY_BASE}${path}`, {
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
  if (query.pinned !== undefined) params.set("pinned", String(query.pinned));
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
  };
}

export async function listLibraryCategories(signal?: AbortSignal): Promise<LibraryCategory[]> {
  return normalizeLibraryCategories(await request("/categories", { signal }));
}

export async function createLibraryCategory(name: string): Promise<LibraryCategory> {
  return normalizeLibraryCategory(await request("/categories", {
    method: "POST",
    body: JSON.stringify({ name: assertLibraryCategoryName(name) }),
  }));
}

export async function updateLibraryCategory(id: string, name: string): Promise<LibraryCategory> {
  return normalizeLibraryCategory(await request(`/categories/${encodeId(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ name: assertLibraryCategoryName(name) }),
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

/** Start (or join) the account's durable metadata backfill job. */
export async function startMetadataBackfill(signal?: AbortSignal): Promise<MetadataBackfillProgress> {
  return normalizeMetadataBackfillProgress(await request("/metadata-backfills", {
    method: "POST",
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
