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

/** 只覆盖界面可见的字段；推迟与静音不影响卡片外观，交给服务端响应。 */
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
  return next;
}
