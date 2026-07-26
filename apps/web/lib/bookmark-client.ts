import {
  bookmarkErrorMessage,
  normalizeBookmarkImportResult,
  normalizeBookmarkImportStatus,
  normalizeBookmarkImportUpload,
  normalizeBookmarkPreviewSummary,
  type BookmarkImportResult,
  type BookmarkImportStatus,
  type BookmarkImportUpload,
  type BookmarkPreviewSummary,
} from "./bookmark-contract.ts";

const BOOKMARK_BASE = "/api/backend/bookmark-imports";

// 后端要求幂等键至少 16 字符；randomUUID 是 36 字符，天然满足。
const MIN_IDEMPOTENCY_KEY_LENGTH = 16;

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

/** 唯一真正写库的一步，必须由用户点击触发。 */
export async function applyBookmarkImport(
  jobId: string,
  expectedJobVersion: number,
): Promise<BookmarkImportResult> {
  return normalizeBookmarkImportResult(
    await request(`/${encodeURIComponent(jobId)}/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_job_version: expectedJobVersion }),
    }),
  );
}
