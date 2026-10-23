// 订阅流（Q50）的前端契约：把 /api/feeds 的响应规范化成页面直接可用的形状，
// 并提供纯函数（时长、错误文案、按来源分组、安全链接），便于 Node 测试覆盖。

import { createContractGuards } from "./contract-guards.ts";
import { parseServerTime } from "./relative-time.ts";

export type FeedKind = "rss" | "podcast";

export type Feed = {
  id: string;
  url: string;
  title: string;
  kind: FeedKind;
  siteUrl: string | null;
  imageUrl: string | null;
  enabled: boolean;
  lastFetchedAt: string | null;
  nextFetchAt: string | null;
  consecutiveFailures: number;
  lastError: string | null;
  itemCount: number;
};

export type FeedItem = {
  id: string;
  feedId: string;
  url: string | null;
  title: string;
  summary: string;
  author: string | null;
  publishedAt: string | null;
  audioUrl: string | null;
  imageUrl: string | null;
  durationSeconds: number | null;
  fetchedAt: string;
  read: boolean;
  dismissed: boolean;
  savedSiteId: string | null;
};

export type FeedPick = {
  item: FeedItem;
  feedTitle: string;
  feedKind: FeedKind;
  reason: string;
  matched: string[];
};

export type FeedSaveResult = { item: FeedItem; siteId: string; created: boolean };

export class FeedsContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FeedsContractError";
  }
}

const { nullableText } = createContractGuards((message) => new FeedsContractError(message));

function fail(path: string, expected: string): never {
  throw new FeedsContractError(`订阅接口返回格式异常：${path} 应为${expected}`);
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) fail(path, "对象");
  return value as Record<string, unknown>;
}

function text(value: unknown, path: string): string {
  if (typeof value !== "string") fail(path, "字符串");
  return value;
}

function optionalText(value: unknown, path: string): string | null {
  if (value === null || value === undefined) return null;
  return text(value, path);
}

function bool(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") fail(path, "布尔值");
  return value;
}

function count(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) fail(path, "非负整数");
  return value;
}

function kind(value: unknown, path: string): FeedKind {
  if (value !== "rss" && value !== "podcast") fail(path, "rss 或 podcast");
  return value;
}

function list(value: unknown, path: string): unknown[] {
  const items = record(value, path).items;
  if (!Array.isArray(items)) fail(`${path}.items`, "数组");
  return items;
}

/** 只放行 http(s) 绝对链接；其余一律当作没有链接，不渲染为 href。 */
export function safeHref(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

export function normalizeFeed(value: unknown, path = "feed"): Feed {
  const raw = record(value, path);
  return {
    id: text(raw.id, `${path}.id`),
    url: text(raw.url, `${path}.url`),
    title: text(raw.title, `${path}.title`),
    kind: kind(raw.kind, `${path}.kind`),
    siteUrl: safeHref(optionalText(raw.site_url, `${path}.site_url`)),
    imageUrl: safeHref(nullableText(raw.image_url, `${path}.image_url`)),
    enabled: bool(raw.enabled, `${path}.enabled`),
    lastFetchedAt: optionalText(raw.last_fetched_at, `${path}.last_fetched_at`),
    nextFetchAt: optionalText(raw.next_fetch_at, `${path}.next_fetch_at`),
    consecutiveFailures: count(raw.consecutive_failures, `${path}.consecutive_failures`),
    lastError: optionalText(raw.last_error, `${path}.last_error`),
    itemCount: count(raw.item_count, `${path}.item_count`),
  };
}

export function normalizeFeedItem(value: unknown, path = "item"): FeedItem {
  const raw = record(value, path);
  const duration = raw.duration_seconds;
  return {
    id: text(raw.id, `${path}.id`),
    feedId: text(raw.feed_id, `${path}.feed_id`),
    url: safeHref(optionalText(raw.url, `${path}.url`)),
    title: text(raw.title, `${path}.title`),
    summary: text(raw.summary, `${path}.summary`),
    author: optionalText(raw.author, `${path}.author`),
    publishedAt: optionalText(raw.published_at, `${path}.published_at`),
    audioUrl: safeHref(optionalText(raw.audio_url, `${path}.audio_url`)),
    imageUrl: safeHref(nullableText(raw.image_url, `${path}.image_url`)),
    durationSeconds: duration === null || duration === undefined
      ? null
      : count(duration, `${path}.duration_seconds`),
    fetchedAt: text(raw.fetched_at, `${path}.fetched_at`),
    read: bool(raw.read, `${path}.read`),
    dismissed: bool(raw.dismissed, `${path}.dismissed`),
    savedSiteId: optionalText(raw.saved_site_id, `${path}.saved_site_id`),
  };
}

export function normalizeFeedList(value: unknown): Feed[] {
  return list(value, "feeds").map((entry, index) => normalizeFeed(entry, `feeds.items[${index}]`));
}

export function normalizeFeedItemList(value: unknown): FeedItem[] {
  return list(value, "feed_items").map((entry, index) =>
    normalizeFeedItem(entry, `feed_items.items[${index}]`),
  );
}

export function normalizeFeedPicks(value: unknown): FeedPick[] {
  return list(value, "picks").map((entry, index) => {
    const path = `picks.items[${index}]`;
    const raw = record(entry, path);
    const matched = raw.matched;
    if (!Array.isArray(matched)) fail(`${path}.matched`, "数组");
    return {
      item: normalizeFeedItem(raw.item, `${path}.item`),
      feedTitle: text(raw.feed_title, `${path}.feed_title`),
      feedKind: kind(raw.feed_kind, `${path}.feed_kind`),
      reason: text(raw.reason, `${path}.reason`),
      matched: matched.map((name, position) => text(name, `${path}.matched[${position}]`)),
    };
  });
}

export function normalizeFeedSave(value: unknown): FeedSaveResult {
  const raw = record(value, "save");
  return {
    item: normalizeFeedItem(raw.item, "save.item"),
    siteId: text(raw.site_id, "save.site_id"),
    created: bool(raw.created, "save.created"),
  };
}

/** 1:02:03 / 45:00 这样的节目时长；没有或无效时返回空串。 */
export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds <= 0) return "";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`;
}

const ERROR_LABELS: Record<string, string> = {
  unsafe_target: "地址不可访问（内网或不安全）",
  http_unavailable: "对方服务暂时不可用",
  http_error: "请求被拒绝或地址不存在",
  too_large: "内容超过 2 MB 上限",
  too_many_redirects: "重定向次数过多",
  network_error: "网络连接失败",
  not_a_feed: "不是 RSS / Atom 订阅地址",
};

export function feedErrorLabel(code: string | null): string {
  if (!code) return "";
  return ERROR_LABELS[code] ?? "拉取失败";
}

export function feedKindLabel(value: FeedKind): string {
  return value === "podcast" ? "播客" : "RSS";
}

/** 条目展示时间：优先发布时间，否则取首次拉取时间。 */
export function itemMoment(item: Pick<FeedItem, "publishedAt" | "fetchedAt">): string {
  return item.publishedAt ?? item.fetchedAt;
}

export type FeedGroup = { feed: Feed; items: FeedItem[] };

/** “全部”视图：按订阅源分组（订阅源顺序保持不变），组内按时间倒序；没有条目的源不出现。 */
export function groupItemsByFeed(feeds: readonly Feed[], items: readonly FeedItem[]): FeedGroup[] {
  const byFeed = new Map<string, FeedItem[]>();
  for (const item of items) {
    if (item.dismissed) continue;
    const bucket = byFeed.get(item.feedId);
    if (bucket) bucket.push(item);
    else byFeed.set(item.feedId, [item]);
  }
  const groups: FeedGroup[] = [];
  for (const feed of feeds) {
    const bucket = byFeed.get(feed.id);
    if (!bucket?.length) continue;
    bucket.sort((left, right) => parseServerTime(itemMoment(right)) - parseServerTime(itemMoment(left)));
    groups.push({ feed, items: bucket });
  }
  return groups;
}

export function hostLabel(url: string | null): string {
  if (!url) return "";
  try {
    return new URL(url).hostname.replace(/^www\./u, "");
  } catch {
    return "";
  }
}
