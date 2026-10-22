"use client";

// 从 library-workspace.tsx 拆出来的部分。拆分依据不是"文件太长"，而是这里的东西
// **不持有任何组件状态**：纯函数、本地类型，以及只吃 props 的展示组件。
// 和 800 行的有状态主组件混在一起时，读的人得先确认它们没闭包捕获才敢改；
// 放在独立模块里，这个疑问一开始就不存在。

import {
  ArrowDown,
  ArrowUp,
  ArrowUpRight,
  ChevronsDown,
  ChevronsUp,
  FileText,
  GripVertical,
  Pencil,
  Pin,
  PinOff,
  Star,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { ResourceCover } from "@/components/discover/resource-cover";
import { useShortcutTarget } from "@/components/keyboard/shortcut-target";
import { DynamicIcon } from "@/components/ui/dynamic-icon";
import {
  SiteFavicon,
} from "@/components/site-favicon";
import { Spinner } from "@/components/react-bits/spinner";
import {
  LibraryApiError,
} from "@/lib/library-client";
import {
  LIBRARY_SITE_KINDS,
  LIBRARY_SITE_STATES,
  librarySiteCardSummary,
} from "@/lib/library-contract";
import type {
  LibrarySite,
  LibrarySiteKind,
  LibrarySiteSelectionItem,
  LibrarySiteState,
  LibrarySort,
} from "@/lib/library-contract";
import { formatRelativeTime } from "@/lib/relative-time";
import { resourceKindLabel, resourceTitle } from "@/lib/resource-cover";
import { isUnopened } from "@/lib/site-usage";

export type ViewMode = "grid" | "list";
export type DialogState =
  | { kind: "create" }
  | { kind: "edit"; site: LibrarySite }
  | { kind: "delete"; site: LibrarySite }
  | { kind: "bulk-delete"; sites: LibrarySiteSelectionItem[] }
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

export function siteHost(site: Pick<LibrarySite, "originalUrl">): string {
  try {
    return new URL(site.originalUrl).hostname.replace(/^www\./, "");
  } catch {
    return site.originalUrl;
  }
}

export type SiteCollectionProps = {
  sites: LibrarySite[];
  viewMode: ViewMode;
  /** 相对时间（“3 天前收藏”）的基准时刻，取数时记录。 */
  now: number;
  /** 只靠正文命中搜索词的条目 → 片段（Q48），卡片据此显示“正文命中”。 */
  contentMatches: Readonly<Record<string, string>>;
  selectionMode: boolean;
  selectionBusy: boolean;
  selectedSiteIds: ReadonlySet<string>;
  quickActionId: string | null;
  onToggleSelected: (siteId: string) => void;
  onOpen: (site: LibrarySite) => void;
  onToggleStar: (site: LibrarySite) => void;
  onMarkSeen: (site: LibrarySite) => void;
  onSnooze: (site: LibrarySite) => void;
  onEdit: (site: LibrarySite) => void;
  onDelete: (site: LibrarySite) => void;
  onTogglePinned: (site: LibrarySite) => void;
  /** 仅在「自定义顺序 + 选中单个分类」时可用；否则拖动/移动没有明确语义 */
  reorderable: boolean;
  reorderBusy: boolean;
  onMove: (site: LibrarySite, to: "top" | "up" | "down" | "bottom") => void;
  onDropBefore: (draggedId: string, beforeSiteId: string | null) => void;
};

type LibrarySiteCardProps = Omit<
  SiteCollectionProps,
  "sites" | "selectedSiteIds" | "quickActionId" | "contentMatches"
> & {
  site: LibrarySite;
  /** 正文命中片段；标题等字段命中或没有搜索词时为 undefined。 */
  contentMatch: string | undefined;
  isSelected: boolean;
  isBusy: boolean;
  isFirst: boolean;
  isLast: boolean;
};

/* 管理模式的收藏卡（方向稿 A）：网格视图带紧凑封面，列表视图是一行高密度条目。
   整张卡由一层绝对定位的链接覆盖（进详情），操作按钮浮在链接之上；
   卡片是键盘快捷键的一项（j / k 移动，o / s / e / z 作用于它）。 */
function LibrarySiteCard({
  site,
  contentMatch,
  viewMode,
  now,
  selectionMode,
  selectionBusy,
  isSelected,
  isBusy,
  isFirst,
  isLast,
  onToggleSelected,
  onOpen,
  onToggleStar,
  onMarkSeen,
  onSnooze,
  onEdit,
  onDelete,
  onTogglePinned,
  reorderable,
  reorderBusy,
  onMove,
  onDropBefore,
}: Readonly<LibrarySiteCardProps>) {
  const title = resourceTitle(site);
  const summary = librarySiteCardSummary(site);
  const unopened = isUnopened(site);
  const saved = now > 0 ? formatRelativeTime(site.createdAt, now) : "";
  const shortcutRef = useShortcutTarget<HTMLElement>((command) => {
    if (selectionMode) return;
    if (command === "open") onOpen(site);
    else if (command === "star") onToggleStar(site);
    else if (command === "seen") {
      if (site.seenAt === null) onMarkSeen(site);
    } else onSnooze(site);
  });

  return (
    <article
      ref={shortcutRef}
      className="library-site-card"
      data-shortcut-item
      data-selected={isSelected || undefined}
      data-selection-mode={selectionMode || undefined}
      data-unopened={unopened || undefined}
      aria-busy={selectionBusy || undefined}
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
          aria-label={`${isSelected ? "取消选择" : "选择"} ${title}`}
          disabled={selectionBusy}
          data-shortcut-focus
          onClick={() => onToggleSelected(site.id)}
        />
      ) : (
        <Link
          className="library-site-card-link"
          href={`/library/${encodeURIComponent(site.id)}`}
          aria-label={`查看 ${title} 的详情`}
          draggable={false}
          data-shortcut-focus
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
            disabled={selectionBusy}
            onChange={() => onToggleSelected(site.id)}
            aria-label={`${isSelected ? "取消选择" : "选择"} ${title}`}
          />
        </label>
      )}
      {reorderable && !selectionMode && (
        <div
          className="library-site-reorder"
          role="group"
          aria-label={`调整 ${title} 的顺序`}
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
            aria-label={`把 ${title} 移到最前`}
            title="移到最前"
          >
            <ChevronsUp aria-hidden="true" />
          </button>
          <button
            className="icon-button"
            type="button"
            disabled={reorderBusy || isFirst}
            onClick={() => onMove(site, "up")}
            aria-label={`把 ${title} 上移`}
            title="上移"
          >
            <ArrowUp aria-hidden="true" />
          </button>
          <button
            className="icon-button"
            type="button"
            disabled={reorderBusy || isLast}
            onClick={() => onMove(site, "down")}
            aria-label={`把 ${title} 下移`}
            title="下移"
          >
            <ArrowDown aria-hidden="true" />
          </button>
          <button
            className="icon-button"
            type="button"
            disabled={reorderBusy || isLast}
            onClick={() => onMove(site, "bottom")}
            aria-label={`把 ${title} 移到最后`}
            title="移到最后"
          >
            <ChevronsDown aria-hidden="true" />
          </button>
        </div>
      )}
      {viewMode === "grid" && (
        <div className="library-site-cover" aria-hidden="true">
          <ResourceCover site={site} size="card" />
        </div>
      )}
      <div className="library-site-card-main">
        {viewMode === "list" && <SiteFavicon url={site.faviconUrl} name={site.name} size={24} />}
        <div className="library-site-copy">
          <span className="library-site-host" title={site.originalUrl}>
            {viewMode === "grid" && <SiteFavicon url={site.faviconUrl} name={site.name} size={16} />}
            <span className="library-site-host-text">{siteHost(site)}</span>
            <span aria-hidden="true">·</span>
            <span className="library-site-kind">{resourceKindLabel(site)}</span>
          </span>
          <div className="library-site-title-row">
            <span className="library-site-title">{title}</span>
            {site.pinned && <Pin className="library-pinned-mark" aria-label="已置顶" />}
          </div>
          {summary && <p className="library-site-description">{summary}</p>}
          {contentMatch !== undefined && (
            <p className="library-site-content-match">
              <span className="library-content-match-badge">
                <FileText aria-hidden="true" />正文命中
              </span>
              {contentMatch && <span className="library-content-match-snippet">{contentMatch}</span>}
            </p>
          )}
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
        <span className="library-site-updated">
          {saved && <span>{saved}收藏</span>}
          {unopened && (
            <span className="library-site-unopened">
              <span className="library-site-unopened-dot" aria-hidden="true" />
              未打开
            </span>
          )}
          {site.source === "agent" && <span className="library-site-via">via Agent</span>}
        </span>
        {!selectionMode && <div className="library-site-actions">
          <button
            className="icon-button"
            type="button"
            onClick={() => onOpen(site)}
            aria-label={`打开原站：${title}`}
            title="打开原站（o）"
          >
            <ArrowUpRight aria-hidden="true" />
          </button>
          <button
            className="icon-button library-star-action"
            type="button"
            aria-pressed={site.starred}
            data-active={site.starred || undefined}
            onClick={() => onToggleStar(site)}
            aria-label={site.starred ? `取消星标：${title}` : `加星标：${title}`}
            title={site.starred ? "取消星标（s）" : "加星标（s）"}
          >
            <Star aria-hidden="true" fill={site.starred ? "currentColor" : "none"} />
          </button>
          <button
            className="icon-button"
            type="button"
            disabled={isBusy}
            onClick={() => onTogglePinned(site)}
            aria-busy={isBusy || undefined}
            aria-label={`${isBusy ? "正在" : ""}${site.pinned ? "取消置顶" : "置顶"} ${title}`}
            title={`${isBusy ? "正在" : ""}${site.pinned ? "取消置顶" : "置顶"}`}
          >
            {isBusy ? (
              <Spinner />
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
            aria-label={`编辑 ${title}`}
            title="编辑"
          >
            <Pencil aria-hidden="true" />
          </button>
          <button
            className="icon-button library-delete-action"
            type="button"
            onClick={() => onDelete(site)}
            aria-label={`删除 ${title}`}
            title="删除"
          >
            <Trash2 aria-hidden="true" />
          </button>
        </div>}
      </footer>
    </article>
  );
}

export function SiteCollection({
  sites,
  selectedSiteIds,
  quickActionId,
  contentMatches,
  ...cardProps
}: Readonly<SiteCollectionProps>) {
  return (
    <div className="library-site-collection" data-view={cardProps.viewMode}>
      {sites.map((site, index) => (
        <LibrarySiteCard
          key={site.id}
          site={site}
          contentMatch={contentMatches[site.id]}
          isSelected={selectedSiteIds.has(site.id)}
          isBusy={quickActionId === site.id}
          isFirst={index === 0}
          isLast={index === sites.length - 1}
          {...cardProps}
        />
      ))}
    </div>
  );
}

export const LIBRARY_SORTS: readonly LibrarySort[] = [
  "created",
  "updated",
  "opened",
  "name",
  "custom",
  "relevance",
];

/** 网格 / 列表偏好（每位访问者本机）。 */
export const LIBRARY_VIEW_MODE_STORAGE_KEY = "webhub:library-view-mode";

export function readStoredViewMode(): ViewMode | null {
  try {
    const stored = window.localStorage.getItem(LIBRARY_VIEW_MODE_STORAGE_KEY);
    return stored === "grid" || stored === "list" ? stored : null;
  } catch {
    return null;
  }
}

export function storeViewMode(mode: ViewMode): void {
  try {
    window.localStorage.setItem(LIBRARY_VIEW_MODE_STORAGE_KEY, mode);
  } catch {
    // 隐私模式或存储被禁用时只在本次会话生效
  }
}

/**
 * 首页、顶栏与详情页用 query 参数把筛选意图带过来（?category= / ?tag= / ?kind= / ?state= /
 * ?space= / ?pinned=1 / ?sort= / ?focus=search / ?q=，快速收藏遇到重复网址时用 ?q= 定位已有收藏）。
 * 只在挂载时读一次，之后由页内交互接管——做双向同步会让每次点筛选都写一条历史记录。
 * 直接读 window.location 而不用 useSearchParams，避免额外的 Suspense 边界。
 */
export function initialIntent(): {
  categoryId: string;
  tagId: string;
  spaceId: string;
  kind: LibrarySiteKind | "";
  state: LibrarySiteState | "";
  pinnedOnly: boolean;
  sort: LibrarySort;
  focusSearch: boolean;
  query: string;
} {
  const empty = {
    categoryId: "",
    tagId: "",
    spaceId: "",
    kind: "" as const,
    state: "" as const,
    pinnedOnly: false,
    sort: "updated" as LibrarySort,
    focusSearch: false,
    query: "",
  };
  if (typeof window === "undefined") return empty;
  const params = new URLSearchParams(window.location.search);
  const sort = params.get("sort");
  const kind = params.get("kind");
  const state = params.get("state");
  return {
    categoryId: params.get("category")?.trim() ?? "",
    tagId: params.get("tag")?.trim() ?? "",
    spaceId: params.get("space")?.trim() ?? "",
    kind: LIBRARY_SITE_KINDS.includes(kind as LibrarySiteKind) ? (kind as LibrarySiteKind) : "",
    state: LIBRARY_SITE_STATES.includes(state as LibrarySiteState) ? (state as LibrarySiteState) : "",
    pinnedOnly: params.get("pinned") === "1",
    sort: LIBRARY_SORTS.includes(sort as LibrarySort) ? (sort as LibrarySort) : "updated",
    focusSearch: params.get("focus") === "search",
    query: params.get("q")?.trim().slice(0, 500) ?? "",
  };
}
