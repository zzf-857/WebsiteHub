// 页面内的收藏变更广播：顶栏快速收藏与发现页分属不同组件树，
// 主卡与画廊也各自持有数据。用 window 事件解耦，避免把状态提到布局层。

import type { LibrarySite } from "./library-contract.ts";

const LIBRARY_CHANGED = "webhub:library-changed";
const SITE_UPDATED = "webhub:site-updated";

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
