"use client";

import { useCallback } from "react";

import { announceSiteUpdated } from "@/lib/library-events";
import { recordSiteOpen, updateSiteState } from "@/lib/library-client";
import type { LibrarySite, LibrarySiteStateInput } from "@/lib/library-contract";
import { withSiteOpened, withSiteState } from "@/lib/site-usage";

// 发现页的使用信号（打开 / 星标 / 已看 / 推迟 / 静音）。
// 一律乐观更新：先广播新状态让主卡与画廊同时变化，再请求接口；
// 失败时广播原状态回滚，由调用方决定提示文案。

// 同一条收藏连续点两次星标时，先发出的请求可能后返回；只认最后一次请求的结果，
// 避免界面在两个状态之间闪回。
const latestRequest = new Map<string, number>();
let requestSequence = 0;

function nowIso(): string {
  return new Date().toISOString();
}

export function useSiteActions() {
  /**
   * 新标签打开原站。必须在点击处理函数里同步调用 window.open，
   * 否则浏览器会当成弹窗拦截；打开记录并行发送，失败不打扰用户。
   */
  const openSite = useCallback((site: LibrarySite): LibrarySite => {
    window.open(site.originalUrl, "_blank", "noopener");
    const optimistic = withSiteOpened(site, nowIso());
    announceSiteUpdated(optimistic);
    void recordSiteOpen(site.id)
      .then((result) => {
        announceSiteUpdated({
          ...optimistic,
          openCount: result.openCount,
          lastOpenedAt: result.lastOpenedAt,
          seenAt: result.seenAt,
        });
      })
      .catch(() => undefined);
    return optimistic;
  }, []);

  /** 返回服务端确认后的收藏；失败时先回滚再抛出原错误。 */
  const changeSiteState = useCallback(
    async (site: LibrarySite, input: LibrarySiteStateInput): Promise<LibrarySite> => {
      requestSequence += 1;
      const request = requestSequence;
      latestRequest.set(site.id, request);
      announceSiteUpdated(withSiteState(site, input, nowIso()));
      try {
        const saved = await updateSiteState(site.id, input);
        if (latestRequest.get(site.id) === request) announceSiteUpdated(saved);
        return saved;
      } catch (error) {
        if (latestRequest.get(site.id) === request) announceSiteUpdated(site);
        throw error;
      }
    },
    [],
  );

  return { openSite, changeSiteState };
}
