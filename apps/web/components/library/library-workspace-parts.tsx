"use client";

// 从 library-workspace.tsx 拆出来的部分。拆分依据不是"文件太长"，而是这里的东西
// **不持有任何组件状态**：纯函数、本地类型，以及只吃 props 的展示组件。
// 和 800 行的有状态主组件混在一起时，读的人得先确认它们没闭包捕获才敢改；
// 放在独立模块里，这个疑问一开始就不存在。

import {
  ArrowDown,
  ArrowUp,
  ChevronsDown,
  ChevronsUp,
  GripVertical,
  LoaderCircle,
  Pencil,
  Pin,
  PinOff,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { DynamicIcon } from "@/components/ui/dynamic-icon";
import {
  SiteFavicon,
} from "@/components/site-favicon";
import {
  LibraryApiError,
} from "@/lib/library-client";
import type {
  LibrarySite,
  LibrarySort,
} from "@/lib/library-contract";

export type ViewMode = "grid" | "list";
export type DialogState =
  | { kind: "create" }
  | { kind: "edit"; site: LibrarySite }
  | { kind: "delete"; site: LibrarySite }
  | { kind: "bulk-delete"; sites: LibrarySite[] }
  | { kind: "taxonomy" }
  | null;

export type SitePageState = {
  items: LibrarySite[];
  nextCursor: string | null;
  matchedCount: number;
  loadingMore: boolean;
};

export const EMPTY_PAGE: SitePageState = {
  items: [],
  nextCursor: null,
  matchedCount: 0,
  loadingMore: false,
};

export const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "Asia/Shanghai",
});

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function isLibraryErrorCode(error: unknown, code: string): error is LibraryApiError {
  return error instanceof LibraryApiError && error.code === code;
}

export function isLibraryNotFound(error: unknown): error is LibraryApiError {
  return error instanceof LibraryApiError && (error.code === "not_found" || error.status === 404);
}

export function appendUniqueSites(current: LibrarySite[], incoming: LibrarySite[]): LibrarySite[] {
  const knownIds = new Set(current.map((site) => site.id));
  return [...current, ...incoming.filter((site) => !knownIds.has(site.id))];
}

export function siteHost(site: LibrarySite): string {
  try {
    return new URL(site.originalUrl).hostname.replace(/^www\./, "");
  } catch {
    return site.originalUrl;
  }
}

export type SiteCollectionProps = {
  sites: LibrarySite[];
  viewMode: ViewMode;
  selectionMode: boolean;
  selectedSiteIds: ReadonlySet<string>;
  quickActionId: string | null;
  onToggleSelected: (siteId: string) => void;
  onEdit: (site: LibrarySite) => void;
  onDelete: (site: LibrarySite) => void;
  onTogglePinned: (site: LibrarySite) => void;
  /** 仅在「自定义顺序 + 选中单个分类」时可用；否则拖动/移动没有明确语义 */
  reorderable: boolean;
  reorderBusy: boolean;
  onMove: (site: LibrarySite, to: "top" | "up" | "down" | "bottom") => void;
  onDropBefore: (draggedId: string, beforeSiteId: string | null) => void;
};

export function SiteCollection({
  sites,
  viewMode,
  selectionMode,
  selectedSiteIds,
  quickActionId,
  onToggleSelected,
  onEdit,
  onDelete,
  onTogglePinned,
  reorderable,
  reorderBusy,
  onMove,
  onDropBefore,
}: Readonly<SiteCollectionProps>) {
  return (
    <div className="library-site-collection" data-view={viewMode}>
      {sites.map((site, index) => {
        const isBusy = quickActionId === site.id;
        const isSelected = selectedSiteIds.has(site.id);
        const isFirst = index === 0;
        const isLast = index === sites.length - 1;
        return (
          <article
            className="library-site-card"
            key={site.id}
            data-selected={isSelected || undefined}
            data-selection-mode={selectionMode || undefined}
            draggable={reorderable && !reorderBusy && !selectionMode}
            onDragStart={(event) => {
              event.dataTransfer.setData("text/plain", site.id);
              event.dataTransfer.effectAllowed = "move";
            }}
            onDragOver={(event) => {
              if (!reorderable) return;
              event.preventDefault();
              event.dataTransfer.dropEffect = "move";
            }}
            onDrop={(event) => {
              if (!reorderable) return;
              event.preventDefault();
              const dragged = event.dataTransfer.getData("text/plain");
              if (dragged && dragged !== site.id) onDropBefore(dragged, site.id);
            }}
          >
            {selectionMode ? (
              <button
                className="library-site-card-link library-site-select-surface"
                type="button"
                aria-label={`${isSelected ? "取消选择" : "选择"} ${site.name}`}
                onClick={() => onToggleSelected(site.id)}
              />
            ) : (
              <Link
                className="library-site-card-link"
                href={`/library/${encodeURIComponent(site.id)}`}
                aria-label={`查看 ${site.name} 的详情`}
                draggable={false}
              />
            )}
            {selectionMode && (
              <label
                className="library-site-checkbox"
                onClick={(event) => event.stopPropagation()}
                onPointerDown={(event) => event.stopPropagation()}
              >
                <input
                  type="checkbox"
                  checked={isSelected}
                  onChange={() => onToggleSelected(site.id)}
                  aria-label={`${isSelected ? "取消选择" : "选择"} ${site.name}`}
                />
              </label>
            )}
            {reorderable && !selectionMode && (
              <div
                className="library-site-reorder"
                role="group"
                aria-label={`调整 ${site.name} 的顺序`}
              >
                {/* 拖拽只是叠加在这组按钮之上：键盘用户永远有一条完整路径。 */}
                <span className="library-reorder-grip" aria-hidden="true">
                  <GripVertical />
                </span>
                <button
                  className="icon-button"
                  type="button"
                  disabled={reorderBusy || isFirst}
                  onClick={() => onMove(site, "top")}
                  aria-label={`把 ${site.name} 移到最前`}
                  title="移到最前"
                >
                  <ChevronsUp aria-hidden="true" />
                </button>
                <button
                  className="icon-button"
                  type="button"
                  disabled={reorderBusy || isFirst}
                  onClick={() => onMove(site, "up")}
                  aria-label={`把 ${site.name} 上移`}
                  title="上移"
                >
                  <ArrowUp aria-hidden="true" />
                </button>
                <button
                  className="icon-button"
                  type="button"
                  disabled={reorderBusy || isLast}
                  onClick={() => onMove(site, "down")}
                  aria-label={`把 ${site.name} 下移`}
                  title="下移"
                >
                  <ArrowDown aria-hidden="true" />
                </button>
                <button
                  className="icon-button"
                  type="button"
                  disabled={reorderBusy || isLast}
                  onClick={() => onMove(site, "bottom")}
                  aria-label={`把 ${site.name} 移到最后`}
                  title="移到最后"
                >
                  <ChevronsDown aria-hidden="true" />
                </button>
              </div>
            )}
            <div className="library-site-card-main">
              <SiteFavicon url={site.faviconUrl} name={site.name} size={viewMode === "grid" ? 32 : 24} />
              <div className="library-site-copy">
                <div className="library-site-title-row">
                  <span className="library-site-title">
                    {site.name}
                  </span>
                  {site.pinned && <Pin className="library-pinned-mark" aria-label="已置顶" />}
                </div>
                <span className="library-site-host" title={site.originalUrl}>
                  {siteHost(site)}
                </span>
                {site.description && <p className="library-site-description">{site.description}</p>}
              </div>
            </div>

            <div className="library-site-taxonomy">
              <span className="library-category-chip">
                <DynamicIcon name={site.category.icon || "Folder"} aria-hidden="true" />
                {site.category.name}
              </span>
              {site.tags.slice(0, 3).map((tag) => (
                <span className="library-tag-chip" key={tag.id}>{tag.name}</span>
              ))}
              {site.tags.length > 3 && (
                <span className="library-tag-overflow" title={site.tags.slice(3).map((tag) => tag.name).join("、")}>
                  +{site.tags.length - 3}
                </span>
              )}
            </div>

            <footer className="library-site-card-footer">
              <span className="library-site-updated">更新于 {dateFormatter.format(new Date(site.updatedAt))}</span>
              {!selectionMode && <div className="library-site-actions">
                <button
                  className="icon-button"
                  type="button"
                  disabled={isBusy}
                  onClick={() => onTogglePinned(site)}
                  aria-label={`${site.pinned ? "取消置顶" : "置顶"} ${site.name}`}
                  title={site.pinned ? "取消置顶" : "置顶"}
                >
                  {isBusy ? (
                    <LoaderCircle className="loading-spinner" aria-hidden="true" />
                  ) : site.pinned ? (
                    <PinOff aria-hidden="true" />
                  ) : (
                    <Pin aria-hidden="true" />
                  )}
                </button>
                <button
                  className="icon-button"
                  type="button"
                  onClick={() => onEdit(site)}
                  aria-label={`编辑 ${site.name}`}
                  title="编辑"
                >
                  <Pencil aria-hidden="true" />
                </button>
                <button
                  className="icon-button library-delete-action"
                  type="button"
                  onClick={() => onDelete(site)}
                  aria-label={`删除 ${site.name}`}
                  title="删除"
                >
                  <Trash2 aria-hidden="true" />
                </button>
              </div>}
            </footer>
          </article>
        );
      })}
    </div>
  );
}

export const LIBRARY_SORTS: readonly LibrarySort[] = [
  "created",
  "updated",
  "name",
  "custom",
  "relevance",
];

/**
 * 首页与顶栏用 query 参数把筛选意图带过来（?category= / ?pinned=1 / ?sort= / ?focus=search）。
 * 只在挂载时读一次，之后由页内交互接管——做双向同步会让每次点筛选都写一条历史记录。
 * 直接读 window.location 而不用 useSearchParams，避免额外的 Suspense 边界。
 */
export function initialIntent(): {
  categoryId: string;
  pinnedOnly: boolean;
  sort: LibrarySort;
  focusSearch: boolean;
} {
  const empty = { categoryId: "", pinnedOnly: false, sort: "updated" as LibrarySort, focusSearch: false };
  if (typeof window === "undefined") return empty;
  const params = new URLSearchParams(window.location.search);
  const sort = params.get("sort");
  return {
    categoryId: params.get("category")?.trim() ?? "",
    pinnedOnly: params.get("pinned") === "1",
    sort: LIBRARY_SORTS.includes(sort as LibrarySort) ? (sort as LibrarySort) : "updated",
    focusSearch: params.get("focus") === "search",
  };
}
