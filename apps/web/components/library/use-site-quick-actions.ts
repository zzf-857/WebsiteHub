"use client";

import { useCallback, useMemo } from "react";

import { useSiteActions } from "@/components/discover/use-site-actions";
import { useToast } from "@/components/ui/toast";
import { recordSiteOpen } from "@/lib/library-client";
import type { LibrarySite, LibrarySiteStateInput } from "@/lib/library-contract";
import { announceSiteUpdated } from "@/lib/library-events";
import { withSiteOpened } from "@/lib/site-usage";

// 发现画廊、收藏库管理视图与详情页共用的快捷操作（2026-10 D6）：
// 乐观更新 + 5 秒撤销提示；失败时 useSiteActions 已经回滚，这里只负责提示文案。
// 星标本身就是开关，不另给撤销提示（与发现页一致）。

/** 推迟的默认天数：“下周再提醒”。 */
export const DEFAULT_SNOOZE_DAYS = 7;

export type SiteQuickActions = {
  /** 新标签打开原站并记录（键盘 o、卡片按钮）。 */
  open: (site: LibrarySite) => LibrarySite;
  /** 只记录打开：原站链接是真正的 <a>，由浏览器自己打开（中键、复制链接都可用）。 */
  recordOpen: (site: LibrarySite) => void;
  toggleStar: (site: LibrarySite) => Promise<LibrarySite | null>;
  markSeen: (site: LibrarySite) => Promise<LibrarySite | null>;
  unmarkSeen: (site: LibrarySite) => Promise<LibrarySite | null>;
  snooze: (site: LibrarySite) => Promise<LibrarySite | null>;
};

export function useSiteQuickActions(): SiteQuickActions {
  const { openSite, changeSiteState } = useSiteActions();
  const toast = useToast();

  const undoable = useCallback(
    (
      site: LibrarySite,
      input: LibrarySiteStateInput,
      undo: LibrarySiteStateInput,
      message: string,
      failure: string,
    ): Promise<LibrarySite | null> =>
      changeSiteState(site, input)
        .then((saved) => {
          toast.show({
            message,
            action: {
              label: "撤销",
              onAction: () => {
                changeSiteState(saved, undo).catch(() => {
                  toast.show({ message: "撤销没有成功，请稍后重试", tone: "danger" });
                });
              },
            },
          });
          return saved;
        })
        .catch(() => {
          toast.show({ message: failure, tone: "danger" });
          return null;
        }),
    [changeSiteState, toast],
  );

  const toggleStar = useCallback(
    (site: LibrarySite) =>
      changeSiteState(site, { starred: !site.starred }).catch(() => {
        toast.show({ message: "星标没有保存，请稍后重试", tone: "danger" });
        return null;
      }),
    [changeSiteState, toast],
  );

  const recordOpen = useCallback((site: LibrarySite) => {
    const optimistic = withSiteOpened(site, new Date().toISOString());
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
  }, []);

  return useMemo(
    () => ({
      open: openSite,
      recordOpen,
      toggleStar,
      markSeen: (site: LibrarySite) =>
        undoable(site, { seen: true }, { seen: false }, "已标记为已看", "没有标记成功，请稍后重试"),
      unmarkSeen: (site: LibrarySite) =>
        undoable(site, { seen: false }, { seen: true }, "已改回未看", "没有改成功，请稍后重试"),
      snooze: (site: LibrarySite) =>
        undoable(
          site,
          { snoozeDays: DEFAULT_SNOOZE_DAYS },
          { snoozeDays: 0 },
          "下周再提醒你",
          "没有推迟成功，请稍后重试",
        ),
    }),
    [openSite, recordOpen, toggleStar, undoable],
  );
}
