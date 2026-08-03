import {
  bookmarkErrorMessage,
  normalizeBookmarkImportResult,
  normalizeBookmarkImportStatus,
  normalizeBookmarkImportUpload,
  normalizeBookmarkPreviewSummary,
  normalizeBookmarkSimilarityClusterPage,
  normalizeBookmarkSimilarityDecisionResult,
  normalizeBookmarkSimilarityMemberPage,
  type BookmarkImportResult,
  type BookmarkImportStatus,
  type BookmarkImportUpload,
  type BookmarkPreviewSummary,
  type BookmarkSimilarityClusterPage,
  type BookmarkSimilarityDecision,
  type BookmarkSimilarityDecisionFilter,
  type BookmarkSimilarityDecisionResult,
  type BookmarkSimilarityMemberPage,
} from "./bookmark-contract.ts";

const BOOKMARK_BASE = "/api/backend/bookmark-imports";

// 后端要求幂等键至少 16 字符；randomUUID 是 36 字符，天然满足。
const MIN_IDEMPOTENCY_KEY_LENGTH = 16;
export const DEFAULT_BOOKMARK_SIMILARITY_PAGE_SIZE = 20;
export const DEFAULT_BOOKMARK_SIMILARITY_MEMBER_PAGE_SIZE = 50;

export class BookmarkApiError extends Error {
  status: number;
  retryAfterSeconds?: number;

  constructor(status: number, message: string, retryAfterSeconds?: number) {
    super(message);
    this.name = "BookmarkApiError";
    this.status = status;
    if (retryAfterSeconds !== undefined) this.retryAfterSeconds = retryAfterSeconds;
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
  const response = await fetch(`${BOOKMARK_BASE}${path}`, {
    ...init,
    cache: "no-store",
    credentials: "include",
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const retryAfter = response.headers.get("retry-after");
    throw new BookmarkApiError(
      response.status,
      bookmarkErrorMessage(response.status, payload),
      retryAfter && /^\d+$/.test(retryAfter.trim()) ? Number(retryAfter.trim()) : undefined,
    );
  }
  return payload;
}

export function newBookmarkIdempotencyKey(): string {
  const key = crypto.randomUUID();
  if (key.length < MIN_IDEMPOTENCY_KEY_LENGTH) {
    // 理论上到不了这里；真到了就说明环境的 randomUUID 不合规，宁可报错也不发一个必然 422 的请求。
    throw new TypeError("生成的幂等键长度不足");
  }
  return key;
}

/**
 * Upload one bookmark export.
 *
 * The body is the raw file, not multipart: the backend streams it straight to a
 * snapshot path while hashing, so wrapping it in a form would mean buffering a
 * multi-megabyte export twice for nothing.
 *
 * The idempotency key is the caller's to keep — retrying a failed upload with
 * the same key replays the original job instead of creating a second snapshot.
 */
export async function uploadBookmarkFile(
  file: File,
  idempotencyKey: string,
): Promise<BookmarkImportUpload> {
  return normalizeBookmarkImportUpload(
    await request("", {
      method: "POST",
      body: file,
      headers: {
        "Content-Type": "text/html",
        "Idempotency-Key": idempotencyKey,
        // 文件名只用于展示，后端会自行清洗；用 encodeURIComponent 避免非 ASCII 破坏头部。
        "X-Bookmark-Filename": encodeURIComponent(file.name),
      },
    }),
  );
}

export async function getBookmarkImportStatus(
  jobId: string,
  signal?: AbortSignal,
): Promise<BookmarkImportStatus> {
  return normalizeBookmarkImportStatus(
    await request(`/${encodeURIComponent(jobId)}`, { signal }),
  );
}

export async function getBookmarkPreviewSummary(
  jobId: string,
  signal?: AbortSignal,
): Promise<BookmarkPreviewSummary> {
  return normalizeBookmarkPreviewSummary(
    await request(`/${encodeURIComponent(jobId)}/preview`, { signal }),
  );
}

type BookmarkSimilarityPageQuery = {
  cursor?: string;
  page?: number;
  limit?: number;
  decision?: BookmarkSimilarityDecisionFilter;
};

function encodeResourceId(value: string, label: string): string {
  const normalized = value.trim();
  if (!normalized) throw new TypeError(`${label} 不能为空`);
  return encodeURIComponent(normalized);
}

function boundedPageSize(value: number | undefined, fallback: number, maximum: number): number {
  if (value === undefined) return fallback;
  if (!Number.isFinite(value)) throw new TypeError("分页大小必须是有限数字");
  return Math.min(maximum, Math.max(1, Math.trunc(value)));
}

export async function getBookmarkSimilarityClusters(
  jobId: string,
  query: BookmarkSimilarityPageQuery = {},
  signal?: AbortSignal,
): Promise<BookmarkSimilarityClusterPage> {
  const requestedPage = query.page ?? 1;
  if (!Number.isSafeInteger(requestedPage) || requestedPage < 1) {
    throw new TypeError("相似书签页码必须是正整数");
  }
  const cursor = query.cursor?.trim();
  if (cursor && query.page !== undefined) {
    throw new TypeError("相似书签页码和游标不能同时使用");
  }
  const params = new URLSearchParams({
    limit: String(
      boundedPageSize(query.limit, DEFAULT_BOOKMARK_SIMILARITY_PAGE_SIZE, 50),
    ),
  });
  if (cursor) params.set("cursor", cursor);
  else params.set("page", String(requestedPage));
  if (query.decision) params.set("decision", query.decision);
  return normalizeBookmarkSimilarityClusterPage(
    await request(
      `/${encodeResourceId(jobId, "导入任务 ID")}/preview/similarity-clusters?${params}`,
      { signal },
    ),
  );
}

export async function getBookmarkSimilarityMembers(
  jobId: string,
  clusterId: string,
  query: Pick<BookmarkSimilarityPageQuery, "cursor" | "limit"> = {},
  signal?: AbortSignal,
): Promise<BookmarkSimilarityMemberPage> {
  const params = new URLSearchParams({
    limit: String(
      boundedPageSize(
        query.limit,
        DEFAULT_BOOKMARK_SIMILARITY_MEMBER_PAGE_SIZE,
        100,
      ),
    ),
  });
  if (query.cursor?.trim()) params.set("cursor", query.cursor.trim());
  return normalizeBookmarkSimilarityMemberPage(
    await request(
      `/${encodeResourceId(jobId, "导入任务 ID")}/preview/similarity-clusters/${encodeResourceId(clusterId, "相似组 ID")}/members?${params}`,
      { signal },
    ),
  );
}

export async function setBookmarkSimilarityDecision(
  jobId: string,
  clusterId: string,
  input: {
    expectedJobVersion: number;
    expectedDecisionVersion: number;
    decision: BookmarkSimilarityDecision;
  },
): Promise<BookmarkSimilarityDecisionResult> {
  return normalizeBookmarkSimilarityDecisionResult(
    await request(
      `/${encodeResourceId(jobId, "导入任务 ID")}/preview/similarity-clusters/${encodeResourceId(clusterId, "相似组 ID")}/decision`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_job_version: input.expectedJobVersion,
          expected_decision_version: input.expectedDecisionVersion,
          decision: input.decision,
        }),
      },
    ),
  );
}

export async function keepOriginalBookmarkSimilarityClusters(
  jobId: string,
  input: { expectedJobVersion: number; expectedDecisionVersion: number },
): Promise<BookmarkSimilarityDecisionResult> {
  return normalizeBookmarkSimilarityDecisionResult(
    await request(
      `/${encodeResourceId(jobId, "导入任务 ID")}/preview/similarity-decisions/keep-originals`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_job_version: input.expectedJobVersion,
          expected_decision_version: input.expectedDecisionVersion,
          decision: "keep_originals",
        }),
      },
    ),
  );
}

/** 唯一真正写库的一步，必须由用户点击触发。 */
export async function applyBookmarkImport(
  jobId: string,
  expectedJobVersion: number,
  expectedDecisionVersion: number,
): Promise<BookmarkImportResult> {
  return normalizeBookmarkImportResult(
    await request(`/${encodeURIComponent(jobId)}/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_job_version: expectedJobVersion,
        expected_decision_version: expectedDecisionVersion,
      }),
    }),
  );
}
