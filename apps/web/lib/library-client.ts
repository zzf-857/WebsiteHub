import {
  assertLibraryCategoryName,
  assertLibraryExpectedVersion,
  assertLibrarySiteCreateInput,
  assertLibrarySiteUpdateInput,
  assertLibraryTagName,
  libraryErrorDetails,
  normalizeCategoryDeletePreview,
  normalizeLibraryCategories,
  normalizeLibraryCategory,
  normalizeLibrarySite,
  normalizeLibrarySitePage,
  normalizeLibraryTag,
  normalizeLibraryTags,
  type LibraryCategory,
  type LibraryCategoryDeletePreview,
  type LibrarySite,
  type LibrarySiteCreateInput,
  type LibrarySitePage,
  type LibrarySiteQuery,
  type LibrarySiteUpdateInput,
  type LibraryTag,
} from "./library-contract.ts";

const LIBRARY_BASE = "/api/backend/library";
export const DEFAULT_LIBRARY_PAGE_SIZE = 24;
export const MAX_LIBRARY_PAGE_SIZE = 100;

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
    ...(normalized.description !== undefined ? { description: normalized.description } : {}),
    ...(normalized.faviconUrl !== undefined ? { favicon_url: normalized.faviconUrl } : {}),
    ...(normalized.categoryId !== undefined ? { category_id: normalized.categoryId } : {}),
    ...(normalized.tagIds !== undefined ? { tag_ids: normalized.tagIds } : {}),
    ...(normalized.pinned !== undefined ? { pinned: normalized.pinned } : {}),
  };
}

function siteUpdatePayload(input: LibrarySiteUpdateInput): Record<string, unknown> {
  const normalized = assertLibrarySiteUpdateInput(input);
  return {
    expected_version: normalized.expectedVersion,
    ...(normalized.name !== undefined ? { name: normalized.name } : {}),
    ...(normalized.url !== undefined ? { url: normalized.url } : {}),
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
