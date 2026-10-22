// 页面内的收藏变更广播：顶栏快速收藏与发现页分属不同组件树，
// 主卡与画廊也各自持有数据。用 window 事件解耦，避免把状态提到布局层。

import type { LibrarySite } from "./library-contract.ts";

const LIBRARY_CHANGED = "webhub:library-changed";
const SITE_UPDATED = "webhub:site-updated";
const LIBRARY_SEARCH = "webhub:library-search";

/** 收藏集合本身变了（新增 / 删除）：订阅方应重新取数。 */
export function announceLibraryChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(LIBRARY_CHANGED));
}

export function subscribeLibraryChanged(listener: () => void): () => void {
  window.addEventListener(LIBRARY_CHANGED, listener);
  return () => window.removeEventListener(LIBRARY_CHANGED, listener);
}

/** 某条收藏的状态变了（打开 / 星标 / 已看）：订阅方就地替换，不重新取数。 */
export function announceSiteUpdated(site: LibrarySite): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent<LibrarySite>(SITE_UPDATED, { detail: site }));
}

export function subscribeSiteUpdated(listener: (site: LibrarySite) => void): () => void {
  const handler = (event: Event) => {
    const site = (event as CustomEvent<LibrarySite>).detail;
    if (site) listener(site);
  };
  window.addEventListener(SITE_UPDATED, handler);
  return () => window.removeEventListener(SITE_UPDATED, handler);
}

/**
 * 让已经挂载的收藏库页按关键词搜索（快速收藏遇到重复网址时定位已有项）。
 * 收藏库只在挂载时读一次 ?q=，同路由的链接跳转不会重挂页面，所以本页内改走事件。
 */
export function announceLibrarySearch(query: string): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent<string>(LIBRARY_SEARCH, { detail: query }));
}

export function subscribeLibrarySearch(listener: (query: string) => void): () => void {
  const handler = (event: Event) => {
    const query = (event as CustomEvent<string>).detail;
    if (typeof query === "string" && query.trim()) listener(query.trim());
  };
  window.addEventListener(LIBRARY_SEARCH, handler);
  return () => window.removeEventListener(LIBRARY_SEARCH, handler);
}
