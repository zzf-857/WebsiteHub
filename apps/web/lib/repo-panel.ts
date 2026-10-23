// 详情页“仓库”面板（Q46）的数据模型。数据来自 Q45 写入的 site.meta.github；
// 没有这份数据时面板整块不渲染，不拿 URL 解析结果冒充统计。纯函数，Node 测试直接覆盖。

import type { LibrarySiteMeta } from "./library-contract.ts";
import { parseServerTime } from "./relative-time.ts";
import { formatCount } from "./format-count.ts";

export type RepoPanelModel = {
  stars: string | null;
  forks: string | null;
  language: string | null;
  topics: string[];
  license: string | null;
  /** “3 天前推送”；没有或无法解析 pushed_at 时为 null。 */
  pushed: string | null;
  pushedAt: string | null;
  archived: boolean;
  /** 没有可展示统计时的状态提示：仓库不存在 / 私有，或 GitHub 限流待重试。 */
  notice: RepoPanelNotice | null;
};

export type RepoPanelNotice = {
  kind: "not_found" | "rate_limited";
  text: string;
};

export const REPO_NOT_FOUND_NOTICE = "仓库不存在或为私有";
export const REPO_RATE_LIMITED_NOTICE = "GitHub 限流，稍后自动重试";

const MAX_TOPICS = 12;
const DAY = 86_400_000;

function nonNegativeCount(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function cleanText(value: unknown, max = 80): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed.slice(0, max) : null;
}

/** GitHub 的 license 可能是 SPDX 字符串，也可能是 {spdx_id, name} 对象；NOASSERTION 视为未知。 */
function licenseLabel(value: unknown): string | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const license = value as Record<string, unknown>;
    return licenseLabel(license.spdx_id) ?? cleanText(license.name);
  }
  const text = cleanText(value);
  return text && text.toUpperCase() !== "NOASSERTION" ? text : null;
}

function topicList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const topics: string[] = [];
  for (const entry of value) {
    const topic = cleanText(entry, 50);
    if (!topic || seen.has(topic.toLowerCase())) continue;
    seen.add(topic.toLowerCase());
    topics.push(topic);
    if (topics.length >= MAX_TOPICS) break;
  }
  return topics;
}

/** 今天推送 / 1 天前推送 / N 天前推送；时钟偏差导致的未来时间按“今天”。 */
export function formatPushedAgo(value: string, now: number): string | null {
  const time = parseServerTime(value);
  if (Number.isNaN(time) || !Number.isFinite(now)) return null;
  const days = Math.floor(Math.max(0, now - time) / DAY);
  return days === 0 ? "今天推送" : `${days} 天前推送`;
}

function githubRecord(meta: LibrarySiteMeta): Record<string, unknown> | null {
  const value = meta.github;
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function emptyModel(notice: RepoPanelNotice): RepoPanelModel {
  return {
    stars: null,
    forks: null,
    language: null,
    topics: [],
    license: null,
    pushed: null,
    pushedAt: null,
    archived: false,
    notice,
  };
}

/**
 * meta.github 存在且至少有一项可展示的字段时返回面板模型；Q45 记过“仓库不存在”或
 * “限流且尚无数据”时返回只带提示的模型；其余情况返回 null（面板不渲染）。
 */
export function repoPanelModel(meta: LibrarySiteMeta, now: number): RepoPanelModel | null {
  const github = githubRecord(meta);
  if (!github) return null;
  // 404 之后旧数字会误导，后端已清掉统计；即便残留也不展示。
  if (github.not_found === true) {
    return emptyModel({ kind: "not_found", text: REPO_NOT_FOUND_NOTICE });
  }
  const stars = nonNegativeCount(github.stars);
  const forks = nonNegativeCount(github.forks);
  const pushedAt = cleanText(github.pushed_at, 64);
  const model: RepoPanelModel = {
    stars: stars === null ? null : formatCount(stars),
    forks: forks === null ? null : formatCount(forks),
    language: cleanText(github.language, 40),
    topics: topicList(github.topics),
    license: licenseLabel(github.license),
    pushed: pushedAt ? formatPushedAgo(pushedAt, now) : null,
    pushedAt: pushedAt && !Number.isNaN(parseServerTime(pushedAt)) ? pushedAt : null,
    archived: github.archived === true,
    notice: null,
  };
  const empty = model.stars === null
    && model.forks === null
    && model.language === null
    && model.topics.length === 0
    && model.license === null
    && model.pushed === null
    && !model.archived;
  if (!empty) return model;
  return cleanText(github.rate_limited_at, 64)
    ? emptyModel({ kind: "rate_limited", text: REPO_RATE_LIMITED_NOTICE })
    : null;
}
