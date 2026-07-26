"use client";

import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  ChevronsDown,
  ChevronsUp,
  ExternalLink,
  FolderCog,
  FolderTree,
  GripVertical,
  LayoutGrid,
  List,
  LoaderCircle,
  Pencil,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  Search,
  Tags,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";

import { LibraryDialog } from "@/components/library/library-dialog";
import { SiteForm } from "@/components/library/site-form";
import { TaxonomyManager } from "@/components/library/taxonomy-manager";
// 全站统一使用共享版网站图标（size 为像素值）；
// 旧枚举尺寸按 small=20 / medium=24 / large=32 迁移
import { SiteFavicon } from "@/components/site-favicon";
import {
  createLibrarySite,
  DEFAULT_LIBRARY_PAGE_SIZE,
  deleteLibrarySite,
  getLibrarySite,
  LibraryApiError,
  listLibraryCategories,
  listLibrarySites,
  listLibraryTags,
  reorderLibrarySites,
  updateLibrarySite,
} from "@/lib/library-client";
import type {
  LibraryCategory,
  LibraryDirection,
  LibrarySite,
  LibrarySiteCreateInput,
  LibrarySiteQuery,
  LibrarySiteUpdateInput,
  LibrarySort,
  LibraryTag,
} from "@/lib/library-contract";

type ViewMode = "grid" | "list";
type DialogState =
  | { kind: "create" }
  | { kind: "edit"; site: LibrarySite }
  | { kind: "delete"; site: LibrarySite }
  | { kind: "taxonomy" }
  | null;

type SitePageState = {
  items: LibrarySite[];
  nextCursor: string | null;
  matchedCount: number;
  loadingMore: boolean;
};

const EMPTY_PAGE: SitePageState = {
  items: [],
  nextCursor: null,
  matchedCount: 0,
  loadingMore: false,
};

const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "Asia/Shanghai",
});

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function isLibraryErrorCode(error: unknown, code: string): error is LibraryApiError {
  return error instanceof LibraryApiError && error.code === code;
}

function isLibraryNotFound(error: unknown): error is LibraryApiError {
  return error instanceof LibraryApiError && (error.code === "not_found" || error.status === 404);
}

function appendUniqueSites(current: LibrarySite[], incoming: LibrarySite[]): LibrarySite[] {
  const knownIds = new Set(current.map((site) => site.id));
  return [...current, ...incoming.filter((site) => !knownIds.has(site.id))];
}

function siteHost(site: LibrarySite): string {
  try {
    return new URL(site.originalUrl).hostname.replace(/^www\./, "");
  } catch {
    return site.originalUrl;
  }
}

type SiteCollectionProps = {
  sites: LibrarySite[];
  viewMode: ViewMode;
  quickActionId: string | null;
  onEdit: (site: LibrarySite) => void;
  onDelete: (site: LibrarySite) => void;
  onTogglePinned: (site: LibrarySite) => void;
  /** 仅在「自定义顺序 + 选中单个分类」时可用；否则拖动/移动没有明确语义 */
  reorderable: boolean;
  reorderBusy: boolean;
  onMove: (site: LibrarySite, to: "top" | "up" | "down" | "bottom") => void;
  onDropBefore: (draggedId: string, beforeSiteId: string | null) => void;
};

function SiteCollection({
  sites,
  viewMode,
  quickActionId,
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
        const isFirst = index === 0;
        const isLast = index === sites.length - 1;
        return (
          <article
            className="library-site-card"
            key={site.id}
            draggable={reorderable && !reorderBusy}
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
            {reorderable && (
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
                  <Link href={`/library/${encodeURIComponent(site.id)}`} className="library-site-title">
                    {site.name}
                  </Link>
                  {site.pinned && <Pin className="library-pinned-mark" aria-label="已置顶" />}
                </div>
                <a
                  className="library-site-host"
                  href={site.originalUrl}
                  target="_blank"
                  rel="noreferrer"
                  title={site.originalUrl}
                >
                  {siteHost(site)}
                </a>
                {site.description && <p className="library-site-description">{site.description}</p>}
              </div>
            </div>

            <div className="library-site-taxonomy">
              <span className="library-category-chip">
                <FolderTree aria-hidden="true" />
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
              <div className="library-site-actions">
                <a
                  className="icon-button"
                  href={site.originalUrl}
                  target="_blank"
                  rel="noreferrer"
                  aria-label={`访问 ${site.name}`}
                  title="访问网站"
                >
                  <ExternalLink aria-hidden="true" />
                </a>
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
              </div>
            </footer>
          </article>
        );
      })}
    </div>
  );
}

const LIBRARY_SORTS: readonly LibrarySort[] = ["created", "updated", "name", "custom"];

/**
 * 首页与顶栏用 query 参数把筛选意图带过来（?category= / ?pinned=1 / ?sort= / ?focus=search）。
 * 只在挂载时读一次，之后由页内交互接管——做双向同步会让每次点筛选都写一条历史记录。
 * 直接读 window.location 而不用 useSearchParams，避免额外的 Suspense 边界。
 */
function initialIntent(): {
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

export function LibraryWorkspace() {
  const [intent] = useState(initialIntent);
  const [categories, setCategories] = useState<LibraryCategory[]>([]);
  const [tags, setTags] = useState<LibraryTag[]>([]);
  const [taxonomyLoading, setTaxonomyLoading] = useState(true);
  const [taxonomyError, setTaxonomyError] = useState<string | null>(null);

  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [categoryId, setCategoryId] = useState(intent.categoryId);
  const [tagId, setTagId] = useState("");
  const [pinnedOnly, setPinnedOnly] = useState(intent.pinnedOnly);
  const [sort, setSort] = useState<LibrarySort>(intent.sort);
  const [direction, setDirection] = useState<LibraryDirection>("desc");
  const searchInputRef = useRef<HTMLInputElement>(null);

  // ⌘K 从顶栏跳过来时应当直接可以打字，否则用户还要再点一次输入框。
  useEffect(() => {
    if (intent.focusSearch) searchInputRef.current?.focus();
  }, [intent.focusSearch]);
  const [viewMode, setViewMode] = useState<ViewMode>("grid");

  const [pinnedPage, setPinnedPage] = useState<SitePageState>(EMPTY_PAGE);
  const [regularPage, setRegularPage] = useState<SitePageState>(EMPTY_PAGE);
  const [sitesLoading, setSitesLoading] = useState(true);
  const [sitesError, setSitesError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const requestGeneration = useRef(0);
  const taxonomyRequestGeneration = useRef(0);

  const [dialog, setDialog] = useState<DialogState>(null);
  const [mutationBusy, setMutationBusy] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [quickActionId, setQuickActionId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reorderBusy, setReorderBusy] = useState(false);

  const loadTaxonomies = useCallback(async (signal?: AbortSignal) => {
    const generation = taxonomyRequestGeneration.current + 1;
    taxonomyRequestGeneration.current = generation;
    setTaxonomyLoading(true);
    setTaxonomyError(null);
    try {
      const [nextCategories, nextTags] = await Promise.all([
        listLibraryCategories(signal),
        listLibraryTags(signal),
      ]);
      if (signal?.aborted || taxonomyRequestGeneration.current !== generation) return;
      setCategories(nextCategories);
      setTags(nextTags);
      setCategoryId((current) => (
        !current || nextCategories.some((category) => category.id === current) ? current : ""
      ));
      setTagId((current) => (!current || nextTags.some((tag) => tag.id === current) ? current : ""));
    } catch (error) {
      if (
        isAbortError(error)
        || signal?.aborted
        || taxonomyRequestGeneration.current !== generation
      ) return;
      setTaxonomyError(errorMessage(error, "分类和标签加载失败，请重试"));
      throw error;
    } finally {
      if (!signal?.aborted && taxonomyRequestGeneration.current === generation) {
        setTaxonomyLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.resolve()
      .then(() => loadTaxonomies(controller.signal))
      .catch(() => undefined);
    return () => controller.abort();
  }, [loadTaxonomies]);

  useEffect(() => {
    const timer = window.setTimeout(() => setSearchQuery(searchInput.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  const siteQuery = useMemo<LibrarySiteQuery>(() => ({
    ...(searchQuery ? { q: searchQuery } : {}),
    ...(categoryId ? { categoryId } : {}),
    ...(tagId ? { tagId } : {}),
    sort,
    direction,
    limit: DEFAULT_LIBRARY_PAGE_SIZE,
  }), [categoryId, direction, searchQuery, sort, tagId]);

  useEffect(() => {
    const controller = new AbortController();
    const generation = requestGeneration.current + 1;
    requestGeneration.current = generation;

    const load = async () => {
      setSitesLoading(true);
      setSitesError(null);
      setPinnedPage(EMPTY_PAGE);
      setRegularPage(EMPTY_PAGE);
      try {
        const [pinned, regular] = await Promise.all([
          listLibrarySites({ ...siteQuery, pinned: true }, controller.signal),
          pinnedOnly
            ? Promise.resolve(null)
            : listLibrarySites({ ...siteQuery, pinned: false }, controller.signal),
        ]);
        if (controller.signal.aborted || requestGeneration.current !== generation) return;
        setPinnedPage({
          items: pinned.items,
          nextCursor: pinned.nextCursor,
          matchedCount: pinned.aggregate.matchedCount,
          loadingMore: false,
        });
        setRegularPage(regular ? {
          items: regular.items,
          nextCursor: regular.nextCursor,
          matchedCount: regular.aggregate.matchedCount,
          loadingMore: false,
        } : EMPTY_PAGE);
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        if (requestGeneration.current === generation) {
          setSitesError(errorMessage(error, "网站列表加载失败，请重试"));
        }
      } finally {
        if (!controller.signal.aborted && requestGeneration.current === generation) {
          setSitesLoading(false);
        }
      }
    };

    void load();
    return () => controller.abort();
  }, [pinnedOnly, refreshVersion, siteQuery]);

  const refreshSites = useCallback(() => {
    setRefreshVersion((current) => current + 1);
  }, []);

  const refreshAfterMutation = useCallback(() => {
    refreshSites();
    void loadTaxonomies().catch(() => undefined);
  }, [loadTaxonomies, refreshSites]);

  const handleTaxonomyChanged = useCallback(async () => {
    try {
      await loadTaxonomies();
    } finally {
      refreshSites();
    }
  }, [loadTaxonomies, refreshSites]);

  const loadMore = async (kind: "pinned" | "regular") => {
    const page = kind === "pinned" ? pinnedPage : regularPage;
    if (!page.nextCursor || page.loadingMore) return;
    const generation = requestGeneration.current;
    const setPage = kind === "pinned" ? setPinnedPage : setRegularPage;
    setPage((current) => ({ ...current, loadingMore: true }));
    setSitesError(null);
    try {
      const result = await listLibrarySites({
        ...siteQuery,
        pinned: kind === "pinned",
        cursor: page.nextCursor,
      });
      if (requestGeneration.current !== generation) return;
      setPage((current) => ({
        items: appendUniqueSites(current.items, result.items),
        nextCursor: result.nextCursor,
        matchedCount: result.aggregate.matchedCount,
        loadingMore: false,
      }));
    } catch (error) {
      if (requestGeneration.current !== generation) return;
      setSitesError(errorMessage(error, "更多网站加载失败，请重试"));
      setPage((current) => ({ ...current, loadingMore: false }));
    }
  };

  const openDialog = (nextDialog: Exclude<DialogState, null>) => {
    setMutationError(null);
    setDialog(nextDialog);
  };

  const closeDialog = () => {
    if (mutationBusy) return;
    setDialog(null);
    setMutationError(null);
  };

  const handleCreate = async (input: LibrarySiteCreateInput) => {
    setMutationBusy(true);
    setMutationError(null);
    try {
      await createLibrarySite(input);
      setDialog(null);
      setNotice("网站已加入资料库");
      refreshAfterMutation();
    } catch (error) {
      setMutationError(errorMessage(error, "新增网站失败，请重试"));
    } finally {
      setMutationBusy(false);
    }
  };

  const refreshConflictedSite = async (
    site: LibrarySite,
    kind: "edit" | "delete",
  ): Promise<void> => {
    try {
      const latestSite = await getLibrarySite(site.id);
      setDialog({ kind, site: latestSite });
      setMutationError(kind === "edit"
        ? "该网站已被其他操作更新，已载入最新版本，请核对后再次保存。"
        : "该网站已被其他操作更新，已载入最新版本，请再次确认删除。");
    } catch (error) {
      if (isLibraryNotFound(error)) {
        setDialog(null);
        setNotice("该网站已被删除，列表已刷新");
      } else {
        setMutationError(errorMessage(error, "读取最新网站信息失败，请关闭后重试"));
      }
    } finally {
      refreshSites();
    }
  };

  const handleUpdate = async (input: LibrarySiteUpdateInput) => {
    if (dialog?.kind !== "edit") return;
    const editingSite = dialog.site;
    setMutationBusy(true);
    setMutationError(null);
    try {
      await updateLibrarySite(editingSite.id, input);
      setDialog(null);
      setNotice("网站信息已更新");
      refreshAfterMutation();
    } catch (error) {
      if (isLibraryErrorCode(error, "version_conflict")) {
        await refreshConflictedSite(editingSite, "edit");
      } else if (isLibraryNotFound(error)) {
        setDialog(null);
        setNotice("该网站已不存在，列表已刷新");
        refreshAfterMutation();
      } else {
        setMutationError(errorMessage(error, "保存失败，请重试"));
      }
    } finally {
      setMutationBusy(false);
    }
  };

  const handleDelete = async () => {
    if (dialog?.kind !== "delete") return;
    const deletingSite = dialog.site;
    setMutationBusy(true);
    setMutationError(null);
    try {
      await deleteLibrarySite(deletingSite.id, deletingSite.version);
      setDialog(null);
      setNotice("网站已从资料库删除");
      refreshAfterMutation();
    } catch (error) {
      if (isLibraryErrorCode(error, "version_conflict")) {
        await refreshConflictedSite(deletingSite, "delete");
      } else if (isLibraryNotFound(error)) {
        setDialog(null);
        setNotice("该网站已不存在，列表已刷新");
        refreshAfterMutation();
      } else {
        setMutationError(errorMessage(error, "删除失败，请重试"));
      }
    } finally {
      setMutationBusy(false);
    }
  };

  const handleTogglePinned = async (site: LibrarySite) => {
    if (quickActionId) return;
    setQuickActionId(site.id);
    setSitesError(null);
    try {
      await updateLibrarySite(site.id, {
        pinned: !site.pinned,
        expectedVersion: site.version,
      });
      setNotice(site.pinned ? "已取消置顶" : "网站已置顶");
      refreshAfterMutation();
    } catch (error) {
      if (isLibraryErrorCode(error, "version_conflict")) {
        setNotice("置顶状态已在其他页面改变，列表已刷新");
        refreshSites();
      } else if (isLibraryNotFound(error)) {
        setNotice("该网站已不存在，列表已刷新");
        refreshAfterMutation();
      } else {
        setSitesError(errorMessage(error, "置顶状态更新失败，请重试"));
      }
    } finally {
      setQuickActionId(null);
    }
  };

  const handleSortChange = (event: ChangeEvent<HTMLSelectElement>) => {
    setSort(event.target.value as LibrarySort);
  };

  const totalMatched = pinnedPage.matchedCount + (pinnedOnly ? 0 : regularPage.matchedCount);
  const totalLibrarySites = categories.reduce((total, category) => total + category.siteCount, 0);
  const hasSites = pinnedPage.items.length > 0 || regularPage.items.length > 0;
  const hasActiveFilters = Boolean(searchQuery || categoryId || tagId || pinnedOnly);
  // 只有「自定义顺序 + 选中单个分类」时才允许重排：跨分类或按名称排序时，
  // 「上移一位」没有可以落库的含义——写进哪个分类的哪个位置？说不清就不给按钮。
  const reorderable = sort === "custom" && Boolean(categoryId);

  const handleReorder = useCallback(
    async (orderedSiteIds: string[], beforeSiteId: string | null) => {
      if (!categoryId || reorderBusy) return;
      setReorderBusy(true);
      try {
        await reorderLibrarySites(categoryId, { orderedSiteIds, beforeSiteId });
        // 重拉而不是就地挪动本地数组：位置由服务端唯一索引裁决，
        // 本地乐观更新一旦与它不一致，用户会看到刷新后顺序又变回去。
        refreshSites();
      } catch (error) {
        setNotice(error instanceof Error ? error.message : "调整顺序失败，请重试。");
      } finally {
        setReorderBusy(false);
      }
    },
    [categoryId, refreshSites, reorderBusy],
  );

  const handleMove = useCallback(
    async (site: LibrarySite, to: "top" | "up" | "down" | "bottom") => {
      const items = regularPage.items;
      const index = items.findIndex((entry) => entry.id === site.id);
      if (index < 0) return;
      // 换算成锚点：上移 = 落在前一个之前；下移 = 落在后一个的后一个之前。
      let anchor: string | null;
      if (to === "top") anchor = items[0]?.id ?? null;
      else if (to === "up") anchor = items[index - 1]?.id ?? null;
      else if (to === "down") anchor = items[index + 2]?.id ?? null;
      else anchor = null;
      if (to === "down" && !items[index + 1]) return;
      await handleReorder([site.id], anchor);
    },
    [handleReorder, regularPage.items],
  );

  const collectionProps = {
    viewMode,
    quickActionId,
    onEdit: (site: LibrarySite) => openDialog({ kind: "edit", site }),
    onDelete: (site: LibrarySite) => openDialog({ kind: "delete", site }),
    onTogglePinned: (site: LibrarySite) => void handleTogglePinned(site),
    reorderable,
    reorderBusy,
    onMove: (site: LibrarySite, to: "top" | "up" | "down" | "bottom") =>
      void handleMove(site, to),
    onDropBefore: (draggedId: string, beforeSiteId: string | null) =>
      void handleReorder([draggedId], beforeSiteId),
  };

  return (
    <main className="site-main library-workspace">
      <header className="workspace-page-header library-page-header">
        <div>
          <span className="page-kicker">WebHub</span>
          <h1>资料库</h1>
          <p>整理、检索并维护当前账号保存的网站。</p>
        </div>
        <div className="library-page-actions">
          <button
            className="library-button secondary"
            type="button"
            onClick={() => openDialog({ kind: "taxonomy" })}
          >
            <FolderCog aria-hidden="true" />
            管理分类与标签
          </button>
          <button className="library-button primary" type="button" onClick={() => openDialog({ kind: "create" })}>
            <Plus aria-hidden="true" />
            新增网站
          </button>
        </div>
      </header>

      <div className="library-layout">
        <aside className="library-sidebar" aria-label="资料库分类">
          <section className="library-sidebar-section">
            <h2>浏览</h2>
            <button
              className="library-sidebar-item"
              type="button"
              data-active={!pinnedOnly && !categoryId || undefined}
              onClick={() => { setPinnedOnly(false); setCategoryId(""); }}
            >
              <span><LayoutGrid aria-hidden="true" />全部网站</span>
              <small>{totalLibrarySites}</small>
            </button>
            <button
              className="library-sidebar-item"
              type="button"
              data-active={pinnedOnly || undefined}
              onClick={() => setPinnedOnly(true)}
            >
              <span><Pin aria-hidden="true" />置顶网站</span>
              <small>{pinnedPage.matchedCount}</small>
            </button>
          </section>

          <section className="library-sidebar-section">
            <div className="library-sidebar-heading">
              <h2>分类</h2>
              {taxonomyLoading && <LoaderCircle className="loading-spinner" aria-label="正在加载分类" />}
            </div>
            <div className="library-category-list">
              {categories.map((category) => (
                <button
                  className="library-sidebar-item"
                  type="button"
                  key={category.id}
                  data-active={categoryId === category.id || undefined}
                  onClick={() => setCategoryId((current) => current === category.id ? "" : category.id)}
                >
                  <span><FolderTree aria-hidden="true" />{category.name}</span>
                  <small>{category.siteCount}</small>
                </button>
              ))}
              {!taxonomyLoading && categories.length === 0 && (
                <p className="library-inline-empty">暂无分类</p>
              )}
            </div>
          </section>
        </aside>

        <div className="library-content">
          <div className="library-toolbar">
            <label className="library-search-field">
              <Search aria-hidden="true" />
              <span className="sr-only">搜索资料库</span>
              <input
                ref={searchInputRef}
                type="search"
                placeholder="搜索名称、网址或描述"
                value={searchInput}
                onChange={(event) => setSearchInput(event.target.value)}
              />
            </label>
            <label className="library-filter-select">
              <Tags aria-hidden="true" />
              <span className="sr-only">按标签筛选</span>
              <select value={tagId} onChange={(event) => setTagId(event.target.value)} disabled={taxonomyLoading}>
                <option value="">全部标签</option>
                {tags.map((tag) => (
                  <option key={tag.id} value={tag.id}>{tag.name} ({tag.siteCount})</option>
                ))}
              </select>
            </label>
            <label className="library-filter-select">
              <span className="sr-only">排序字段</span>
              <select value={sort} onChange={handleSortChange}>
                <option value="updated">最近更新</option>
                <option value="created">创建时间</option>
                <option value="name">网站名称</option>
                <option value="custom">自定义顺序</option>
              </select>
            </label>
            <button
              className="icon-button library-direction-button"
              type="button"
              onClick={() => setDirection((current) => current === "asc" ? "desc" : "asc")}
              aria-label={direction === "asc" ? "当前升序，切换为降序" : "当前降序，切换为升序"}
              title={direction === "asc" ? "升序" : "降序"}
            >
              {direction === "asc" ? <ArrowUp aria-hidden="true" /> : <ArrowDown aria-hidden="true" />}
            </button>
            <div className="library-view-toggle" role="group" aria-label="视图模式">
              <button
                className="icon-button"
                type="button"
                data-active={viewMode === "grid" || undefined}
                onClick={() => setViewMode("grid")}
                aria-label="网格视图"
                title="网格视图"
              >
                <LayoutGrid aria-hidden="true" />
              </button>
              <button
                className="icon-button"
                type="button"
                data-active={viewMode === "list" || undefined}
                onClick={() => setViewMode("list")}
                aria-label="列表视图"
                title="列表视图"
              >
                <List aria-hidden="true" />
              </button>
            </div>
          </div>

          <div className="library-results-heading" aria-live="polite">
            <span>{sitesLoading ? "正在读取资料库" : `共 ${totalMatched} 个结果`}</span>
            {hasActiveFilters && (
              <button
                type="button"
                onClick={() => {
                  setSearchInput("");
                  setSearchQuery("");
                  setCategoryId("");
                  setTagId("");
                  setPinnedOnly(false);
                }}
              >
                清除筛选
              </button>
            )}
          </div>

          {notice && (
            <div className="library-notice" role="status">
              <span>{notice}</span>
              <button type="button" onClick={() => setNotice(null)}>关闭</button>
            </div>
          )}

          {taxonomyError && (
            <div className="library-error-banner" role="alert">
              <AlertCircle aria-hidden="true" />
              <span>{taxonomyError}</span>
              <button type="button" onClick={() => void loadTaxonomies().catch(() => undefined)}>
                <RefreshCw aria-hidden="true" />重试
              </button>
            </div>
          )}

          {sitesError && (
            <div className="library-error-banner" role="alert">
              <AlertCircle aria-hidden="true" />
              <span>{sitesError}</span>
              <button type="button" onClick={refreshSites}>
                <RefreshCw aria-hidden="true" />重试
              </button>
            </div>
          )}

          {sitesLoading && !hasSites ? (
            <div className="library-loading-grid" aria-label="正在加载网站">
              {Array.from({ length: 6 }, (_, index) => (
                <div className="library-site-skeleton" key={index} aria-hidden="true" />
              ))}
            </div>
          ) : !hasSites && !sitesError ? (
            <section className="library-empty-state">
              <span className="library-empty-icon" aria-hidden="true"><Search /></span>
              <h2>{hasActiveFilters ? "没有匹配的网站" : "资料库还是空的"}</h2>
              <p>{hasActiveFilters ? "调整关键词或筛选条件后再试。" : "新增第一个网站，开始建立个人网站知识库。"}</p>
              {hasActiveFilters ? (
                <button
                  className="library-button secondary"
                  type="button"
                  onClick={() => {
                    setSearchInput("");
                    setSearchQuery("");
                    setCategoryId("");
                    setTagId("");
                    setPinnedOnly(false);
                  }}
                >
                  清除筛选
                </button>
              ) : (
                <button className="library-button primary" type="button" onClick={() => openDialog({ kind: "create" })}>
                  <Plus aria-hidden="true" />新增网站
                </button>
              )}
            </section>
          ) : (
            <div className="library-sections">
              {pinnedPage.items.length > 0 && (
                <section className="library-pinned-section">
                  <div className="library-section-heading">
                    <div>
                      <Pin aria-hidden="true" />
                      <h2>置顶网站</h2>
                    </div>
                    <span>{pinnedPage.matchedCount} 个</span>
                  </div>
                  <SiteCollection sites={pinnedPage.items} {...collectionProps} />
                  {pinnedPage.nextCursor && (
                    <button
                      className="library-load-more"
                      type="button"
                      onClick={() => void loadMore("pinned")}
                      disabled={pinnedPage.loadingMore}
                    >
                      {pinnedPage.loadingMore && <LoaderCircle className="loading-spinner" aria-hidden="true" />}
                      {pinnedPage.loadingMore ? "正在加载" : "加载更多置顶网站"}
                    </button>
                  )}
                </section>
              )}

              {!pinnedOnly && regularPage.items.length > 0 && (
                <section className="library-regular-section">
                  <div className="library-section-heading">
                    <div>
                      <LayoutGrid aria-hidden="true" />
                      <h2>{pinnedPage.matchedCount > 0 ? "其他网站" : "全部网站"}</h2>
                    </div>
                    <span>{regularPage.matchedCount} 个</span>
                  </div>
                  <SiteCollection sites={regularPage.items} {...collectionProps} />
                  {regularPage.nextCursor && (
                    <button
                      className="library-load-more"
                      type="button"
                      onClick={() => void loadMore("regular")}
                      disabled={regularPage.loadingMore}
                    >
                      {regularPage.loadingMore && <LoaderCircle className="loading-spinner" aria-hidden="true" />}
                      {regularPage.loadingMore ? "正在加载" : "加载更多网站"}
                    </button>
                  )}
                </section>
              )}
            </div>
          )}
        </div>
      </div>

      <LibraryDialog
        open={dialog?.kind === "create" || dialog?.kind === "edit"}
        title={dialog?.kind === "edit" ? "编辑网站" : "新增网站"}
        description={dialog?.kind === "edit" ? "修改资料库中的网站信息。" : "保存一个网站到当前账号的资料库。"}
        size="wide"
        onClose={closeDialog}
      >
        {dialog?.kind === "create" && (
          <SiteForm
            categories={categories}
            tags={tags}
            busy={mutationBusy}
            error={mutationError}
            onCancel={closeDialog}
            onCreate={handleCreate}
          />
        )}
        {dialog?.kind === "edit" && (
          <SiteForm
            site={dialog.site}
            categories={categories}
            tags={tags}
            busy={mutationBusy}
            error={mutationError}
            onCancel={closeDialog}
            onUpdate={handleUpdate}
          />
        )}
      </LibraryDialog>

      <LibraryDialog
        open={dialog?.kind === "delete"}
        title="删除网站"
        description="此操作会从当前账号的资料库中移除该网站。"
        onClose={closeDialog}
      >
        {dialog?.kind === "delete" && (
          <div className="library-delete-confirmation">
            <div className="library-delete-site">
              <SiteFavicon url={dialog.site.faviconUrl} name={dialog.site.name} size={32} />
              <div>
                <strong>{dialog.site.name}</strong>
                <span>{siteHost(dialog.site)}</span>
              </div>
            </div>
            <p>删除后，该网站将不再出现在资料库和关联浏览视图中。</p>
            {mutationError && <p className="library-form-error" role="alert">{mutationError}</p>}
            <footer className="library-form-actions">
              <button className="library-button secondary" type="button" onClick={closeDialog} disabled={mutationBusy}>取消</button>
              <button className="library-button danger" type="button" onClick={() => void handleDelete()} disabled={mutationBusy}>
                {mutationBusy ? <LoaderCircle className="loading-spinner" aria-hidden="true" /> : <Trash2 aria-hidden="true" />}
                {mutationBusy ? "正在删除" : "确认删除"}
              </button>
            </footer>
          </div>
        )}
      </LibraryDialog>

      <LibraryDialog
        open={dialog?.kind === "taxonomy"}
        title="管理分类与标签"
        description="分类删除前会预览受影响的网站；标签删除也会显示关联数量。"
        size="wide"
        onClose={closeDialog}
      >
        {dialog?.kind === "taxonomy" && (
          <TaxonomyManager categories={categories} tags={tags} onChanged={handleTaxonomyChanged} />
        )}
      </LibraryDialog>
    </main>
  );
}
