// 使用信号的乐观更新（打开 / 星标 / 已看）。与 Q42 后端语义保持一致：
// 打开会顺带补上 seen_at；“未打开” = 从未打开且没有标记已看。

import type { LibrarySite, LibrarySiteStateInput } from "./library-contract.ts";

export function isUnopened(site: Pick<LibrarySite, "openCount" | "seenAt">): boolean {
  return site.openCount === 0 && site.seenAt === null;
}

export function withSiteOpened(site: LibrarySite, nowIso: string): LibrarySite {
  return {
    ...site,
    openCount: site.openCount + 1,
    lastOpenedAt: nowIso,
    seenAt: site.seenAt ?? nowIso,
  };
}

const DAY_MS = 86_400_000;

/**
 * 乐观更新使用信号。推迟也先在本地算出到期时间（服务端响应会覆盖为准确值）：
 * 发现页主卡队列靠它即时判断“已被推迟走”，撤销时清空它把该项放回（Q52）。
 */
export function withSiteState(
  site: LibrarySite,
  input: LibrarySiteStateInput,
  nowIso: string,
): LibrarySite {
  const next = { ...site };
  if (input.starred !== undefined) {
    next.starred = input.starred;
    next.starredAt = input.starred ? site.starredAt ?? nowIso : null;
  }
  if (input.seen !== undefined) next.seenAt = input.seen ? site.seenAt ?? nowIso : null;
  if (input.resurfaceMuted !== undefined) next.resurfaceMuted = input.resurfaceMuted;
  if (input.snoozeDays !== undefined) {
    const base = Date.parse(nowIso);
    next.resurfaceDueAt = input.snoozeDays > 0 && Number.isFinite(base)
      ? new Date(base + input.snoozeDays * DAY_MS).toISOString()
      : null;
  }
  return next;
}

/**
 * 画廊就地替换只接收使用信号（打开 / 已看 / 星标 / 静音 / 推迟）。服务端的状态响应是整条收藏，
 * 可能带着后台刚整理好的类型、简介或预览图；整条替换会换掉封面、改变估算高度，
 * 瀑布流按新高度重排，已经放好的卡片整体跳列。内容字段留到下次取数再更新。
 */
export function withSiteUsage(current: LibrarySite, incoming: LibrarySite): LibrarySite {
  if (current.id !== incoming.id) return current;
  return {
    ...current,
    openCount: incoming.openCount,
    lastOpenedAt: incoming.lastOpenedAt,
    seenAt: incoming.seenAt,
    starred: incoming.starred,
    starredAt: incoming.starredAt,
    resurfaceMuted: incoming.resurfaceMuted,
    resurfaceCount: incoming.resurfaceCount,
    resurfaceDueAt: incoming.resurfaceDueAt,
  };
}
