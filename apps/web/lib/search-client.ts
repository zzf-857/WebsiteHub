import {
  normalizeSemanticIndexRun,
  normalizeSemanticIndexStatus,
  type SemanticIndexRun,
  type SemanticIndexStatus,
} from "./search-contract.ts";

// 同 provider-client：next.config.ts 把 /api/backend/* 重写到后端 /api/*。
const SEARCH_BASE = "/api/backend/search";

export class SearchApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "SearchApiError";
    this.status = status;
  }
}

async function readJson(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) return null;
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function messageFrom(payload: unknown, fallback: string): string {
  if (typeof payload === "object" && payload !== null) {
    const detail = (payload as Record<string, unknown>).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return fallback;
}

async function request(path: string, init: RequestInit = {}): Promise<unknown> {
  const response = await fetch(`${SEARCH_BASE}${path}`, {
    ...init,
    // 索引进度会被后台任务改变，读缓存等于给用户看一个不动的进度条。
    cache: "no-store",
    credentials: "include",
  });
  const payload = await readJson(response);
  if (!response.ok) {
    throw new SearchApiError(response.status, messageFrom(payload, "语义索引请求失败"));
  }
  return payload;
}

export async function loadSemanticIndexStatus(
  signal?: AbortSignal,
): Promise<SemanticIndexStatus> {
  return normalizeSemanticIndexStatus(await request("/index", { signal }));
}

export async function rebuildSemanticIndex(input: {
  dropExisting: boolean;
  limit: number;
}): Promise<SemanticIndexRun> {
  return normalizeSemanticIndexRun(
    await request("/index/rebuild", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ drop_existing: input.dropExisting, limit: input.limit }),
    }),
  );
}
