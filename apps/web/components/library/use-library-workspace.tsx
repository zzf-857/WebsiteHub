"use client";

// 从 library-workspace.tsx 抽出来的有状态逻辑。
//
// 拆分动机是可读性而不是行数：原组件把 366 行的 state/effect/handler 和 347 行 JSX
// 摞在一个函数里，改标记要先翻过整套数据加载。分开之后，"数据怎么来"和
// "长什么样"各看各的一个文件。
//
// 返回值有 43 个，看着多，但这就是原来那 366 行本来就要交给 JSX 的东西——
// 拆分只是把这份契约显式写出来，没有新增耦合。

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";
import {
  backfillLibrarySiteMetadata,
  createLibrarySite,
  DEFAULT_LIBRARY_PAGE_SIZE,
  deleteLibrarySite,
  getLibrarySite,
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
import {
  DialogState,
  EMPTY_PAGE,
  SitePageState,
  ViewMode,
  appendUniqueSites,
  errorMessage,
  initialIntent,
  isAbortError,
  isLibraryErrorCode,
  isLibraryNotFound,
} from "@/components/library/library-workspace-parts";

// 全站统一使用共享版网站图标（size 为像素值）；
// 旧枚举尺寸按 small=20 / medium=24 / large=32 迁移

const ANALYSIS_REFRESH_DELAYS_MS = [1_000, 2_000, 3_000, 5_000, 8_000, 13_000] as const;

export function useLibraryWorkspace() {
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
  const [analysisBackfillBusy, setAnalysisBackfillBusy] = useState(false);
  const analysisRefreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

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

  const stopAnalysisRefresh = useCallback(() => {
    if (analysisRefreshTimer.current !== null) {
      clearTimeout(analysisRefreshTimer.current);
      analysisRefreshTimer.current = null;
    }
  }, []);

  const startAnalysisRefresh = useCallback(() => {
    stopAnalysisRefresh();
    refreshSites();
    let index = 0;
    const tick = () => {
      refreshSites();
      const delay = ANALYSIS_REFRESH_DELAYS_MS[index];
      index += 1;
      if (delay === undefined) {
        analysisRefreshTimer.current = null;
        return;
      }
      analysisRefreshTimer.current = setTimeout(tick, delay);
    };
    analysisRefreshTimer.current = setTimeout(tick, ANALYSIS_REFRESH_DELAYS_MS[index]);
    index += 1;
  }, [refreshSites, stopAnalysisRefresh]);

  useEffect(() => stopAnalysisRefresh, [stopAnalysisRefresh]);

  const refreshAfterMutation = useCallback(() => {
    refreshSites();
    void loadTaxonomies().catch(() => undefined);
  }, [loadTaxonomies, refreshSites]);

  const handleAnalysisBackfill = useCallback(async () => {
    if (analysisBackfillBusy) return;
    setAnalysisBackfillBusy(true);
    setSitesError(null);
    try {
      const result = await backfillLibrarySiteMetadata();
      if (result.queuedCount > 0) {
        setNotice(
          result.remainingCount > 0
            ? `已开始补全 ${result.queuedCount} 个网站，另有 ${result.remainingCount} 个可继续处理`
            : `已开始补全 ${result.queuedCount} 个网站`,
        );
      } else if (result.activeCount > 0) {
        setNotice(`已有 ${result.activeCount} 个网站正在补全`);
      } else {
        setNotice("没有待补全的网站");
      }
      if (result.queuedCount > 0 || result.activeCount > 0) {
        startAnalysisRefresh();
      } else {
        refreshSites();
      }
    } catch (error) {
      setSitesError(errorMessage(error, "网站信息补全任务启动失败，请重试"));
    } finally {
      setAnalysisBackfillBusy(false);
    }
  }, [analysisBackfillBusy, refreshSites, startAnalysisRefresh]);

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

  return {
    analysisBackfillBusy,
    categories,
    categoryId,
    closeDialog,
    collectionProps,
    dialog,
    direction,
    handleCreate,
    handleAnalysisBackfill,
    handleDelete,
    handleSortChange,
    handleTaxonomyChanged,
    handleUpdate,
    hasActiveFilters,
    hasSites,
    loadMore,
    loadTaxonomies,
    mutationBusy,
    mutationError,
    notice,
    openDialog,
    pinnedOnly,
    pinnedPage,
    refreshSites,
    regularPage,
    searchInput,
    searchInputRef,
    setCategoryId,
    setDirection,
    setNotice,
    setPinnedOnly,
    setSearchInput,
    setSearchQuery,
    setTagId,
    setViewMode,
    sitesError,
    sitesLoading,
    sort,
    tagId,
    tags,
    taxonomyError,
    taxonomyLoading,
    totalLibrarySites,
    totalMatched,
    viewMode,
  };
}
