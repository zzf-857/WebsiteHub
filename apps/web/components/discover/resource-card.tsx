"use client";

import { ArrowUpRight, Check, Star } from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import Link from "next/link";
import { useState } from "react";

import { ResourceCover } from "@/components/discover/resource-cover";
import { useShortcutTarget } from "@/components/keyboard/shortcut-target";
import { SiteFavicon } from "@/components/site-favicon";
import type { LibrarySite } from "@/lib/library-contract";
import { siteDetailHref } from "@/lib/quick-add";
import { formatRelativeTime } from "@/lib/relative-time";
import { cardOverlayText, resourceTitle, siteDomain } from "@/lib/resource-cover";
import { isUnopened } from "@/lib/site-usage";

// 发现模式的资源卡（方向稿 B）：没有外框，封面 + 标题区。
// 整张卡是一个指向详情页的链接；悬停 / 聚焦时封面上浮出摘要与快捷操作，
// 快捷操作是独立按钮，叠在封面上但不嵌套在链接里（嵌套交互元素既非法也难以键盘操作）。
// 结构用 subgrid：链接占两行（封面 / 标题区），遮罩只占第一行，高度天然等于封面。

type ResourceCardProps = {
  site: LibrarySite;
  /** 渲染相对时间的基准时刻（取数时记录），避免渲染期读时钟。 */
  now: number;
  onOpen: (site: LibrarySite) => void;
  onToggleStar: (site: LibrarySite) => void;
  onMarkSeen: (site: LibrarySite) => void;
  /** 键盘 z：推迟到下周再提醒（卡片上没有对应按钮，避免悬停层过挤）。 */
  onSnooze: (site: LibrarySite) => void;
  /** 键盘 j / k 的数据顺序。 */
  shortcutOrder: number;
};

// motion 的 transition 读不到 CSS 变量，这里复写 --ease-standard 的数值
const EASE_STANDARD: [number, number, number, number] = [0.2, 0.8, 0.2, 1];

function StarGlyph({ starred }: Readonly<{ starred: boolean }>) {
  const reducedMotion = useReducedMotion();
  return (
    <motion.span
      key={starred ? "on" : "off"}
      className="resource-card-star-glyph"
      initial={reducedMotion || !starred ? false : { scale: 0.6 }}
      // 星标点亮时复用 PopCheck 的弹入曲线 0.6 → 1.08 → 1（--duration-pop）
      animate={{ scale: [0.6, 1.08, 1] }}
      transition={reducedMotion || !starred ? { duration: 0 } : { duration: 0.24, times: [0, 0.7, 1], ease: EASE_STANDARD }}
    >
      <Star aria-hidden="true" fill={starred ? "currentColor" : "none"} />
    </motion.span>
  );
}

export function ResourceCard({
  site,
  now,
  onOpen,
  onToggleStar,
  onMarkSeen,
  onSnooze,
  shortcutOrder,
}: Readonly<ResourceCardProps>) {
  const title = resourceTitle(site);
  const domain = siteDomain(site.originalUrl);
  const summary = cardOverlayText(site);
  const unopened = isUnopened(site);
  const seen = site.seenAt !== null;
  const saved = formatRelativeTime(site.createdAt, now);
  // 只有点亮星标那一下才弹，初次渲染与取消星标保持静止
  const [starTouched, setStarTouched] = useState(false);
  const shortcutRef = useShortcutTarget<HTMLElement>((command) => {
    if (command === "open") onOpen(site);
    else if (command === "star") {
      setStarTouched(true);
      onToggleStar(site);
    } else if (command === "seen") {
      if (!seen) onMarkSeen(site);
    } else onSnooze(site);
  });

  return (
    <article
      ref={shortcutRef}
      className="resource-card"
      data-unopened={unopened || undefined}
      data-shortcut-item
      data-shortcut-order={shortcutOrder}
    >
      <Link className="resource-card-link" href={siteDetailHref(site.id)} data-shortcut-focus>
        <div className="resource-card-media">
          <div className="resource-card-cover">
            <ResourceCover site={site} size="card" />
          </div>
          {unopened && (
            <span className="resource-card-badge">
              <span className="resource-card-badge-dot" aria-hidden="true" />
              未打开
            </span>
          )}
          {site.starred && (
            <span className="resource-card-starred" role="img" aria-label="已星标">
              <Star aria-hidden="true" fill="currentColor" />
            </span>
          )}
        </div>
        <div className="resource-card-caption">
          <span className="resource-card-title">{title}</span>
          <span className="resource-card-meta">
            <SiteFavicon url={site.faviconUrl} name={site.name} size={16} />
            <span className="resource-card-domain">{domain}</span>
            {saved && (
              <>
                <span aria-hidden="true">·</span>
                <span className="resource-card-time">{saved}</span>
              </>
            )}
            {site.source === "agent" && (
              <>
                <span aria-hidden="true">·</span>
                <span className="resource-card-via">via Agent</span>
              </>
            )}
          </span>
        </div>
      </Link>

      <div className="resource-card-overlay">
        {summary && <p className="resource-card-summary">{summary}</p>}
        <div className="resource-card-actions">
          <button
            className="resource-card-open"
            type="button"
            onClick={() => onOpen(site)}
            aria-label={`打开原站：${title}`}
          >
            打开
            <ArrowUpRight aria-hidden="true" />
          </button>
          <button
            className="resource-card-icon-action"
            type="button"
            aria-pressed={site.starred}
            aria-label={site.starred ? `取消星标：${title}` : `加星标：${title}`}
            title={site.starred ? "取消星标" : "加星标"}
            data-active={site.starred || undefined}
            onClick={() => {
              setStarTouched(true);
              onToggleStar(site);
            }}
          >
            {starTouched ? (
              <StarGlyph starred={site.starred} />
            ) : (
              <Star aria-hidden="true" fill={site.starred ? "currentColor" : "none"} />
            )}
          </button>
          <button
            className="resource-card-icon-action"
            type="button"
            aria-label={seen ? `已看过：${title}` : `标记已看：${title}`}
            title={seen ? "已看过" : "标记已看"}
            data-active={seen || undefined}
            // 已看过时不用 disabled：按钮被禁用会丢失焦点，遮罩随 focus-within 一起消失
            aria-disabled={seen || undefined}
            onClick={() => {
              if (!seen) onMarkSeen(site);
            }}
          >
            <Check aria-hidden="true" />
          </button>
        </div>
      </div>
    </article>
  );
}
