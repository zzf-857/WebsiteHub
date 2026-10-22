import { agentErrorDetails } from "./agent-access-contract.ts";
import {
  normalizeFeed,
  normalizeFeedItem,
  normalizeFeedItemList,
  normalizeFeedList,
  normalizeFeedPicks,
  normalizeFeedSave,
  type Feed,
  type FeedItem,
  type FeedPick,
  type FeedSaveResult,
} from "./feeds-contract.ts";

// next.config.ts 把 /api/backend/* 重写到后端 /api/*。
const BACKEND = "/api/backend";

export class FeedsApiError extends Error {
  status: number;
  code?: string;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = "FeedsApiError";
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
  const response = await fetch(`${BACKEND}${path}`, {
    ...init,
    cache: "no-store",
    credentials: "include",
    headers: init.body ? { "Content-Type": "application/json", ...init.headers } : init.headers,
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const error = agentErrorDetails(response.status, payload);
    throw new FeedsApiError(response.status, error.message, error.code);
  }
  return payload;
}

function encodeId(id: string): string {
  const normalized = id.trim();
  if (!normalized) throw new TypeError("ID 不能为空");
  return encodeURIComponent(normalized);
}

export async function listFeeds(signal?: AbortSignal): Promise<Feed[]> {
  return normalizeFeedList(await request("/feeds", { signal }));
}

export async function addFeed(url: string): Promise<Feed> {
  const trimmed = url.trim();
  if (!trimmed) throw new TypeError("订阅地址不能为空");
  return normalizeFeed(
    await request("/feeds", { method: "POST", body: JSON.stringify({ url: trimmed }) }),
  );
}

export async function setFeedEnabled(id: string, enabled: boolean): Promise<Feed> {
  return normalizeFeed(
    await request(`/feeds/${encodeId(id)}`, { method: "PATCH", body: JSON.stringify({ enabled }) }),
  );
}

export async function refreshFeed(id: string): Promise<Feed> {
  return normalizeFeed(await request(`/feeds/${encodeId(id)}/refresh`, { method: "POST" }));
}

export async function deleteFeed(id: string): Promise<void> {
  await request(`/feeds/${encodeId(id)}`, { method: "DELETE" });
}

export async function listFeedItems(signal?: AbortSignal): Promise<FeedItem[]> {
  return normalizeFeedItemList(await request("/feeds/items", { signal }));
}

export async function listFeedPicks(signal?: AbortSignal): Promise<FeedPick[]> {
  return normalizeFeedPicks(await request("/feeds/picks", { signal }));
}

export async function updateFeedItem(
  id: string,
  state: { read?: boolean; dismissed?: boolean },
): Promise<FeedItem> {
  return normalizeFeedItem(
    await request(`/feeds/items/${encodeId(id)}`, { method: "PATCH", body: JSON.stringify(state) }),
  );
}

export async function saveFeedItem(id: string): Promise<FeedSaveResult> {
  return normalizeFeedSave(await request(`/feeds/items/${encodeId(id)}/save`, { method: "POST" }));
}
