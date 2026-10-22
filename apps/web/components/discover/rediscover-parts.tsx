"use client";

import { CircleAlert, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import { ResourceCover } from "@/components/discover/resource-cover";
import { Spinner } from "@/components/react-bits/spinner";
import type { HeroEntry } from "@/lib/discover-hero";
import { resourceTitle } from "@/lib/resource-cover";

// “今日重温”的静态小件：骨架、空状态、错误、“接下来”缩略图、分页条。
// 拆出来让 rediscover-section 只管数据流与动效。

export const REDISCOVER_TITLE_ID = "discover-rediscover-title";

export function RediscoverEyebrow({ children }: Readonly<{ children?: ReactNode }>) {
  return (
    <div className="discover-hero-eyebrow">
      <h2 className="discover-eyebrow-title" id={REDISCOVER_TITLE_ID}>
        今日重温
      </h2>
      {children}
    </div>
  );
}

/** 与最终布局同形的骨架：左侧主封面、右侧文字列与三张缩略图。 */
export function RediscoverSkeleton() {
  return (
    <div className="discover-hero" aria-busy="true">
      <div className="discover-hero-cover discover-skeleton" />
      <div className="discover-hero-body">
        <RediscoverEyebrow>
          <span className="sr-only">正在挑选今天的重温</span>
        </RediscoverEyebrow>
        <div className="discover-skeleton-stack" aria-hidden="true">
          <span className="discover-skeleton discover-skeleton-line" style={{ width: "46%" }} />
          <span className="discover-skeleton discover-skeleton-title" style={{ width: "92%" }} />
          <span className="discover-skeleton discover-skeleton-title" style={{ width: "64%" }} />
          <span className="discover-skeleton discover-skeleton-line" style={{ width: "100%" }} />
          <span className="discover-skeleton discover-skeleton-line" style={{ width: "96%" }} />
          <span className="discover-skeleton discover-skeleton-line" style={{ width: "72%" }} />
          <span className="discover-skeleton-actions">
            <span className="discover-skeleton discover-skeleton-button" style={{ width: 96 }} />
            <span className="discover-skeleton discover-skeleton-button" style={{ width: 84 }} />
          </span>
        </div>
        <div className="discover-hero-spacer" />
        <div className="discover-hero-next" aria-hidden="true">
          <span className="discover-skeleton discover-skeleton-line" style={{ width: 48 }} />
          <div className="discover-hero-thumbs">
            {[0, 1, 2].map((index) => (
              <span className="discover-hero-thumb-skeleton" key={index}>
                <span className="discover-skeleton discover-skeleton-thumb" />
                <span className="discover-skeleton discover-skeleton-line" style={{ width: "80%" }} />
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

type RediscoverNoticeProps = {
  tone: "empty" | "done" | "error";
  message: string;
  actionLabel?: string;
  busy?: boolean;
  onAction?: () => void;
};

/** 空 / 处理完 / 出错三种安静的提示面板，高度与一行缩略图相当。 */
export function RediscoverNotice({ tone, message, actionLabel, busy = false, onAction }: Readonly<RediscoverNoticeProps>) {
  return (
    <div className="discover-notice" data-tone={tone} role={tone === "error" ? "alert" : undefined}>
      {tone === "error" && <CircleAlert className="discover-notice-icon" aria-hidden="true" />}
      <p className="discover-notice-text">{message}</p>
      {actionLabel && onAction && (
        <button className="discover-text-button" type="button" onClick={onAction} disabled={busy}>
          {busy ? <Spinner size={14} /> : <RefreshCw aria-hidden="true" />}
          {actionLabel}
        </button>
      )}
    </div>
  );
}

type PagerProps = {
  entries: readonly HeroEntry[];
  activeId: string | null;
  onSelect: (id: string) => void;
};

/** 4 根分页条：每根都是按钮，点击把该条提为主卡，不跳转。 */
export function RediscoverPager({ entries, activeId, onSelect }: Readonly<PagerProps>) {
  if (entries.length < 2) return null;
  return (
    <div className="discover-pager" role="group" aria-label="切换重温条目">
      {entries.map((entry, index) => {
        const active = entry.site.id === activeId;
        return (
          <button
            key={entry.site.id}
            className="discover-pager-bar"
            type="button"
            data-active={active || undefined}
            aria-current={active ? "true" : undefined}
            aria-label={`第 ${index + 1} 条：${resourceTitle(entry.site)}`}
            onClick={() => onSelect(entry.site.id)}
          />
        );
      })}
    </div>
  );
}

type UpNextProps = {
  entries: readonly HeroEntry[];
  onSelect: (id: string) => void;
};

export function RediscoverUpNext({ entries, onSelect }: Readonly<UpNextProps>) {
  if (entries.length === 0) return null;
  return (
    <div className="discover-hero-thumbs">
      {entries.map((entry) => {
        const title = resourceTitle(entry.site);
        return (
          <button
            key={entry.site.id}
            className="discover-hero-thumb"
            type="button"
            aria-label={`换到这条：${title}`}
            onClick={() => onSelect(entry.site.id)}
          >
            <span className="discover-hero-thumb-cover">
              <ResourceCover site={entry.site} size="thumb" />
            </span>
            <span className="discover-hero-thumb-title">{title}</span>
          </button>
        );
      })}
    </div>
  );
}
