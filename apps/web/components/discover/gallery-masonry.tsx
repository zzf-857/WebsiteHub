"use client";

import { motion, useReducedMotion } from "motion/react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ResourceCard } from "@/components/discover/resource-card";
import type { LibrarySite } from "@/lib/library-contract";
import { layoutMasonry, masonryColumnCount } from "@/lib/masonry";
import { estimateCardHeight } from "@/lib/resource-cover";

// 瀑布流的渲染层：ResizeObserver 量容器宽度 → 列数，lib/masonry 按估算高度分列。
// 列宽只在列数变化时更新：同一列数下拖动窗口不会让卡片换列。

export const GALLERY_COLUMN_GAP = 24;
export const GALLERY_ROW_GAP = 28;
/** 首屏错峰入场的上限：再往后的卡片同时出现，避免最后一张等太久。 */
const MAX_STAGGER_STEPS = 12;

// motion 的 transition 读不到 CSS 变量，这里复写 --ease-standard 的数值
const EASE_STANDARD: [number, number, number, number] = [0.2, 0.8, 0.2, 1];

export type GalleryEntrance = Readonly<Record<string, number | "fade">>;

type Grid = { count: number; width: number };

type GalleryMasonryProps = {
  items: readonly LibrarySite[];
  now: number;
  /** 本次需要入场动效的卡片：数字为首屏错峰序号，"fade" 为追加页的整体淡入。 */
  entrance: GalleryEntrance;
  onOpen: (site: LibrarySite) => void;
  onToggleStar: (site: LibrarySite) => void;
  onMarkSeen: (site: LibrarySite) => void;
  onSnooze: (site: LibrarySite) => void;
};

function columnWidth(grid: Grid): number {
  return (grid.width - GALLERY_COLUMN_GAP * (grid.count - 1)) / grid.count;
}

/** 量容器宽度；列数没变时保留上次的宽度，估算高度因此保持稳定。 */
export function useGalleryGrid() {
  const ref = useRef<HTMLDivElement | null>(null);
  const [grid, setGrid] = useState<Grid | null>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = (width: number) => {
      const count = masonryColumnCount(width);
      setGrid((current) => (current && current.count === count ? current : { count, width }));
    };
    if (typeof ResizeObserver === "undefined") {
      measure(element.getBoundingClientRect().width);
      return;
    }
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width !== undefined) measure(width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return { ref, grid };
}

function GalleryItem({
  site,
  entrance,
  children,
}: Readonly<{ site: LibrarySite; entrance: number | "fade" | undefined; children: ReactNode }>) {
  const reducedMotion = useReducedMotion();
  // 始终渲染 motion.div：initial 只在挂载时生效，入场标记清掉后不会重挂卡片
  const still = reducedMotion || entrance === undefined;
  const delay = entrance === undefined || entrance === "fade"
    ? 0
    : (Math.min(entrance, MAX_STAGGER_STEPS) * 60) / 1000;
  return (
    <motion.div
      className="discover-gallery-item"
      data-site-id={site.id}
      initial={still ? false : entrance === "fade" ? { opacity: 0 } : { opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      // 单项时长对齐 --duration-pop，逐项延迟 60ms（与 StaggerList 一致）
      transition={still ? { duration: 0 } : { duration: 0.24, delay, ease: EASE_STANDARD }}
    >
      {children}
    </motion.div>
  );
}

export function GalleryMasonry({
  items,
  now,
  entrance,
  onOpen,
  onToggleStar,
  onMarkSeen,
  onSnooze,
  grid,
}: Readonly<GalleryMasonryProps & { grid: Grid }>) {
  const byId = useMemo(() => new Map(items.map((site) => [site.id, site])), [items]);
  const orderById = useMemo(() => new Map(items.map((site, index) => [site.id, index])), [items]);
  const layout = useMemo(() => {
    const width = columnWidth(grid);
    return layoutMasonry(
      items.map((site) => ({ id: site.id, height: estimateCardHeight(site, width) })),
      grid.count,
      GALLERY_ROW_GAP,
    );
  }, [items, grid]);

  return (
    <div className="discover-gallery-columns" data-columns={grid.count}>
      {layout.columns.map((column, columnIndex) => (
        <div className="discover-gallery-column" key={columnIndex}>
          {column.map((id) => {
            const site = byId.get(id);
            if (!site) return null;
            return (
              <GalleryItem key={id} site={site} entrance={entrance[id]}>
                <ResourceCard
                  site={site}
                  now={now}
                  // 瀑布流 DOM 按列排，j / k 要按收藏顺序走（Q46）
                  shortcutOrder={orderById.get(id) ?? 0}
                  onOpen={onOpen}
                  onToggleStar={onToggleStar}
                  onMarkSeen={onMarkSeen}
                  onSnooze={onSnooze}
                />
              </GalleryItem>
            );
          })}
        </div>
      ))}
    </div>
  );
}

/** 首屏骨架：按列数铺几张不同高度的占位卡，和真实瀑布流的节奏一致。 */
export function GallerySkeleton({ count }: Readonly<{ count: number }>) {
  const heights = [196, 176, 214, 168, 200, 186];
  return (
    <div className="discover-gallery-columns" aria-hidden="true">
      {Array.from({ length: count }, (_, columnIndex) => (
        <div className="discover-gallery-column" key={columnIndex}>
          {[0, 1].map((row) => (
            <div className="discover-gallery-skeleton-card" key={row}>
              <span
                className="discover-skeleton discover-gallery-skeleton-cover"
                style={{ height: heights[(columnIndex * 2 + row) % heights.length] }}
              />
              <span className="discover-skeleton discover-skeleton-line" style={{ width: "86%" }} />
              <span className="discover-skeleton discover-skeleton-line" style={{ width: "52%" }} />
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
