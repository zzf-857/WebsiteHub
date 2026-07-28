"use client";

import { ChevronRight, Clock } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import {
  ErrorNotice,
  errorText,
  isAbortError,
  SectionHeading,
} from "@/components/home/home-shared";
import { SiteFavicon } from "@/components/site-favicon";
import { listLibrarySites } from "@/lib/library-client";
import { librarySiteCardSummary } from "@/lib/library-contract";
import type { LibrarySite } from "@/lib/library-contract";

// 首页「最近收录」分区（设计稿 1a 行 137–156）：单卡片内的行列表。

type FetchState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; sites: LibrarySite[] };

const ROW_COUNT = 5;
const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;

/**
 * 相对时间：刚刚 / N 小时前 / 昨天 / N 天前 / 上周 / YYYY-MM-DD。
 * 纯函数并接受 now 注入，方便单测覆盖各分支边界。
 */
export function formatRelativeTime(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";

  // 服务器时钟略快时 diff 为负，按「刚刚」处理而不是显示未来时间
  const diffMs = now.getTime() - date.getTime();
  if (diffMs < HOUR_MS) return "刚刚";

  const hours = Math.floor(diffMs / HOUR_MS);
  if (hours < 24) return `${hours} 小时前`;

  // 跨天判断按本地自然日算：昨晚 23 点收录的，今天凌晨也应显示「昨天」
  const startOfDay = (d: Date) =>
    new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(now) - startOfDay(date)) / DAY_MS);
  if (dayDiff <= 1) return "昨天";
  if (dayDiff < 7) return `${dayDiff} 天前`;
  if (dayDiff < 14) return "上周";

  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function RecentSites() {
  const [state, setState] = useState<FetchState>({ status: "loading" });
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      setState({ status: "loading" });
      try {
        const page = await listLibrarySites(
          { sort: "created", direction: "desc", limit: ROW_COUNT },
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setState({ status: "ready", sites: page.items });
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        setState({ status: "error", message: errorText(error, "最近收录加载失败") });
      }
    };
    void load();
    return () => controller.abort();
  }, [reloadKey]);

  return (
    <section className="home-section" aria-label="最近收录">
      <SectionHeading
        icon={Clock}
        title="最近收录"
        actionHref="/library?sort=created"
        actionLabel="查看更多 →"
      />

      {state.status === "loading" && (
        <div className="home-recent-card" aria-hidden="true">
          {Array.from({ length: ROW_COUNT }, (_, index) => (
            <div className="home-recent-row home-skeleton-card" key={index}>
              <span className="home-skeleton-block" style={{ width: 20, height: 20 }} />
              <span className="home-skeleton-bar" style={{ width: 96, height: 12 }} />
              <span
                className="home-skeleton-bar"
                style={{ flex: 1, maxWidth: 320, height: 11 }}
              />
              <span className="home-skeleton-bar" style={{ width: 48, height: 18 }} />
              <span className="home-skeleton-bar" style={{ width: 64, height: 11 }} />
            </div>
          ))}
        </div>
      )}

      {state.status === "error" && (
        <ErrorNotice
          message={state.message}
          onRetry={() => setReloadKey((key) => key + 1)}
        />
      )}

      {state.status === "ready" && state.sites.length === 0 && (
        <p className="home-empty">网址库还是空的，先收录第一个网站吧。</p>
      )}

      {state.status === "ready" && state.sites.length > 0 && (
        <div className="home-recent-card">
          {state.sites.map((site) => (
            <Link
              key={site.id}
              className="home-recent-row"
              href={`/library/${encodeURIComponent(site.id)}`}
              title={site.name}
            >
              <span className="home-recent-favicon">
                <SiteFavicon url={site.faviconUrl} name={site.name} size={20} />
              </span>
              <span className="home-recent-name">{site.name}</span>
              <span className="home-recent-desc">{librarySiteCardSummary(site)}</span>
              <span className="home-recent-cat">{site.category.name}</span>
              <span className="home-recent-time">{formatRelativeTime(site.createdAt)}</span>
              <ChevronRight className="home-recent-arrow" aria-hidden="true" />
            </Link>
          ))}
        </div>
      )}
    </section>
  );
}
