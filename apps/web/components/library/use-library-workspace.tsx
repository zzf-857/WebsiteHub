"use client";

// 从 library-workspace.tsx 抽出来的有状态逻辑。
//
// 拆分动机是可读性而不是行数：原组件把 366 行的 state/effect/handler 和 347 行 JSX
// 摞在一个函数里，改标记要先翻过整套数据加载。分开之后，"数据怎么来"和
// "长什么样"各看各的一个文件。
//
// 返回项看着多，但这就是原来那 366 行本来就要交给 JSX 的东西——
// 拆分只是把这份契约显式写出来，没有新增耦合。

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";
import {
  createLibraryTagResolvingConflict,
  createLibrarySite,
  DEFAULT_LIBRARY_PAGE_SIZE,
  deleteLibrarySites,
  deleteLibrarySite,
  getLibrarySite,
  listLibraryCategories,
  listLibrarySiteSelection,
  listLibrarySites,
  listLibraryTags,
  MAX_LIBRARY_PAGE_SIZE,
  reorderLibrarySites,
  startMetadataBackfill,
  updateLibrarySite,
} from "@/lib/library-client";
import {
  isMetadataBackfillTerminalStatus,
  MAX_LIBRARY_BULK_DELETE_SITES,
} from "@/lib/library-contract";
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
import { canStartLibraryPageLoad } from "@/lib/library-pagination";
import {
  hasRefreshableSiteAnalysis,
  useBoundedAnalysisRefresh,
} from "@/lib/analysis-refresh";
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
import { useLibrarySelection } from "@/components/library/use-library-selection";

// 全站统一使用共享版网站图标（size 为像素值）；
// 旧枚举尺寸按 small=20 / medium=24 / large=32 迁移

type PageKind = "pinned" | "regular";

type AnalysisRefreshWaiter = {
  intent: number;
  resolve: () => void;
};

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
  const loadedPagesRef = useRef({ pinned: pinnedPage, regular: regularPage });
  const preserveLoadedSitesOnRefresh = useRef(false);
  const refreshIntentRef = useRef(0);
  const analysisRefreshWaiterRef = useRef<AnalysisRefreshWaiter | null>(null);
  const [sitesLoading, setSitesLoading] = useState(true);
  const [sitesError, setSitesError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const requestGeneration = useRef(0);
  const paginationControllers = useRef<Record<PageKind, AbortController | null>>({
    pinned: null,
    regular: null,
  });
  const paginationInFlight = useRef<Record<PageKind, boolean>>({
    pinned: false,
    regular: false,
  });
  const paginationFailedCursor = useRef<Record<PageKind, string | null>>({
    pinned: null,
    regular: null,
  });
  const selectionSnapshotController = useRef<AbortController | null>(null);
  const taxonomyRequestGeneration = useRef(0);

  const [dialog, setDialog] = useState<DialogState>(null);
  const [mutationBusy, setMutationBusy] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [quickActionId, setQuickActionId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reorderBusy, setReorderBusy] = useState(false);
  const [analysisBackfillBusy, setAnalysisBackfillBusy] = useState(false);
  const [allMatchingSelectionBusy, setAllMatchingSelectionBusy] = useState(false);
  const [bulkDeleteCompleted, setBulkDeleteCompleted] = useState(0);

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
  const selectionScope = useMemo(
    () => JSON.stringify({ pinnedOnly, ...siteQuery }),
    [pinnedOnly, siteQuery],
  );
  const activeSiteQueryScope = useRef(selectionScope);
  useLayoutEffect(() => {
    selectionSnapshotController.current?.abort();
    activeSiteQueryScope.current = selectionScope;
    preserveLoadedSitesOnRefresh.current = false;
  }, [selectionScope]);
  useLayoutEffect(() => {
    loadedPagesRef.current = { pinned: pinnedPage, regular: regularPage };
  }, [pinnedPage, regularPage]);
  const {
    allLoadedSelected,
    allMatchingSelected,
    clearSelectedSites,
    clearSelection: clearStoredSelection,
    loadedSiteCount,
    retainVisibleSelection,
    selectedSiteIds,
    selectedSites,
    selectionMode,
    selectAllMatchingSites,
    setSelectionMode,
    toggleAllLoadedSites,
    toggleSiteSelection,
  } = useLibrarySelection({
    scope: selectionScope,
    pinnedSites: pinnedPage.items,
    regularSites: regularPage.items,
  });

  const clearSelection = useCallback(() => {
    selectionSnapshotController.current?.abort();
    selectionSnapshotController.current = null;
    setAllMatchingSelectionBusy(false);
    clearStoredSelection();
  }, [clearStoredSelection]);

  useLayoutEffect(() => {
    // A selection snapshot belongs to exactly one filter scope. Clear the
    // stored state as well as hiding selection mode, otherwise returning to a
    // previous filter could revive stale expected_version values.
    clearSelection();
  }, [clearSelection, selectionScope]);

  const cancelPaginationRequests = useCallback((clearFailedCursors = false) => {
    for (const kind of ["pinned", "regular"] as const) {
      paginationControllers.current[kind]?.abort();
      paginationControllers.current[kind] = null;
      paginationInFlight.current[kind] = false;
      if (clearFailedCursors) paginationFailedCursor.current[kind] = null;
    }
  }, []);

  useEffect(() => {
    cancelPaginationRequests(true);
    const controller = new AbortController();
    const generation = requestGeneration.current + 1;
    requestGeneration.current = generation;
    const preserveLoadedSites = preserveLoadedSitesOnRefresh.current;
    preserveLoadedSitesOnRefresh.current = false;
    const analysisWaiter = analysisRefreshWaiterRef.current?.intent === refreshVersion
      ? analysisRefreshWaiterRef.current
      : null;
    const settleAnalysisWaiter = () => {
      if (analysisWaiter !== null && analysisRefreshWaiterRef.current === analysisWaiter) {
        analysisRefreshWaiterRef.current = null;
        analysisWaiter.resolve();
      }
    };

    const load = async () => {
      if (!preserveLoadedSites) setSitesLoading(true);
      setSitesError(null);
      if (!preserveLoadedSites) {
        setPinnedPage(EMPTY_PAGE);
        setRegularPage(EMPTY_PAGE);
      }
      try {
        const previous = loadedPagesRef.current;
        const loadedLimit = (kind: PageKind) => Math.min(
          MAX_LIBRARY_PAGE_SIZE,
          Math.max(DEFAULT_LIBRARY_PAGE_SIZE, previous[kind].items.length),
        );
        const [pinned, regular] = await Promise.all([
          listLibrarySites({
            ...siteQuery,
            pinned: true,
            ...(preserveLoadedSites ? { limit: loadedLimit("pinned") } : {}),
          }, controller.signal),
          pinnedOnly
            ? Promise.resolve(null)
            : listLibrarySites({
                ...siteQuery,
                pinned: false,
                ...(preserveLoadedSites ? { limit: loadedLimit("regular") } : {}),
              }, controller.signal),
        ]);
        if (controller.signal.aborted || requestGeneration.current !== generation) return;
        const pinnedItems = preserveLoadedSites
          ? appendUniqueSites(pinned.items, previous.pinned.items)
          : pinned.items;
        const regularItems = regular
          ? (preserveLoadedSites
              ? appendUniqueSites(regular.items, previous.regular.items)
              : regular.items)
          : [];
        // A metadata refresh never changes a site's user-visible sort key. If
        // the user already scrolled past the API's maximum one-page limit,
        // replacing this cursor with the first page's cursor would make the
        // next automatic load repeat an old page and stall the sentinel.
        const pinnedNextCursor = preserveLoadedSites && previous.pinned.items.length > 0
          ? previous.pinned.nextCursor
          : pinned.nextCursor;
        const regularNextCursor = regular === null
          ? null
          : preserveLoadedSites && previous.regular.items.length > 0
            ? previous.regular.nextCursor
            : regular.nextCursor;
        setPinnedPage({
          items: pinnedItems,
          nextCursor: pinnedNextCursor,
          matchedCount: pinned.aggregate.matchedCount,
          loadingMore: false,
        });
        setRegularPage(regular ? {
          items: regularItems,
          nextCursor: regularNextCursor,
          matchedCount: regular.aggregate.matchedCount,
          loadingMore: false,
        } : EMPTY_PAGE);
        loadedPagesRef.current = {
          pinned: {
            items: pinnedItems,
            nextCursor: pinnedNextCursor,
            matchedCount: pinned.aggregate.matchedCount,
            loadingMore: false,
          },
          regular: regular ? {
            items: regularItems,
            nextCursor: regularNextCursor,
            matchedCount: regular.aggregate.matchedCount,
            loadingMore: false,
          } : EMPTY_PAGE,
        };
        const refreshedSites = [...pinnedItems, ...regularItems];
        if (!preserveLoadedSites) retainVisibleSelection(refreshedSites);
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        if (requestGeneration.current === generation) {
          setSitesError(errorMessage(error, "网站列表加载失败，请重试"));
        }
      } finally {
        if (!controller.signal.aborted && requestGeneration.current === generation) {
          setSitesLoading(false);
          settleAnalysisWaiter();
        }
      }
    };

    void load();
    return () => {
      controller.abort();
      settleAnalysisWaiter();
    };
  }, [
    cancelPaginationRequests,
    pinnedOnly,
    refreshVersion,
    retainVisibleSelection,
    selectionScope,
    siteQuery,
  ]);

  useEffect(() => () => cancelPaginationRequests(), [cancelPaginationRequests]);
  useEffect(() => () => selectionSnapshotController.current?.abort(), []);

  const triggerSitesRefresh = useCallback((preserveLoadedSites: boolean, waitForLoad = false) => {
    preserveLoadedSitesOnRefresh.current = preserveLoadedSites;
    requestGeneration.current += 1;
    cancelPaginationRequests(true);
    const intent = refreshIntentRef.current + 1;
    refreshIntentRef.current = intent;
    const previousWaiter = analysisRefreshWaiterRef.current;
    if (previousWaiter !== null) {
      analysisRefreshWaiterRef.current = null;
      previousWaiter.resolve();
    }
    setRefreshVersion(intent);
    if (!waitForLoad) return undefined;
    return new Promise<void>((resolve) => {
      analysisRefreshWaiterRef.current = { intent, resolve };
    });
  }, [cancelPaginationRequests]);

  const refreshSites = useCallback(() => {
    triggerSitesRefresh(false);
  }, [triggerSitesRefresh]);
  const refreshSitesForAnalysis = useCallback(() => {
    return triggerSitesRefresh(true, true);
  }, [triggerSitesRefresh]);

  const analysisRefreshEnabled = useMemo(
    () => !sitesLoading && hasRefreshableSiteAnalysis([...pinnedPage.items, ...regularPage.items]),
    [pinnedPage.items, regularPage.items, sitesLoading],
  );

  useBoundedAnalysisRefresh({
    scope: selectionScope,
    enabled: analysisRefreshEnabled,
    refresh: refreshSitesForAnalysis,
  });

  const refreshAfterMutation = useCallback(() => {
    refreshSites();
    void loadTaxonomies().catch(() => undefined);
  }, [loadTaxonomies, refreshSites]);

  const handleCreateTag = useCallback(async (name: string): Promise<LibraryTag> => {
    const result = await createLibraryTagResolvingConflict(name);
    setTags((current) => {
      if (result.latestTags !== null) return result.latestTags;
      const next = current.filter((tag) => tag.id !== result.tag.id);
      next.push(result.tag);
      next.sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
      return next;
    });
    return result.tag;
  }, []);

  const handleAnalysisBackfill = useCallback(async () => {
    if (analysisBackfillBusy) return;
    setAnalysisBackfillBusy(true);
    setSitesError(null);
    try {
      const result = await startMetadataBackfill();
      if (result.status === "completed") {
        setNotice(
          result.totalCount === 0
            ? "没有需要补全的网站"
            : `网站信息补全已完成 ${result.completedCount} / ${result.totalCount}`,
        );
      } else if (isMetadataBackfillTerminalStatus(result.status)) {
        setNotice(
          result.stoppedEarly
            ? "网站信息补全已提前停止，请检查模型或搜索 Provider、搜索服务批量能力后重试"
            : `网站信息补全存在失败项（${result.completedCount} / ${result.totalCount}），可再次补全`,
        );
      } else {
        setNotice(
          result.reused
            ? `已加入网站信息补全任务（${result.completedCount} / ${result.totalCount}）`
            : `已开始网站信息补全（共 ${result.totalCount} 个），进度可在首页查看`,
        );
      }
      refreshSites();
    } catch (error) {
      setSitesError(errorMessage(error, "网站信息补全任务启动失败，请重试"));
    } finally {
      setAnalysisBackfillBusy(false);
    }
  }, [analysisBackfillBusy, refreshSites]);

  const handleTaxonomyChanged = useCallback(async () => {
    try {
      await loadTaxonomies();
    } finally {
      refreshSites();
    }
  }, [loadTaxonomies, refreshSites]);

  const loadMore = useCallback(async (kind: PageKind) => {
    const page = kind === "pinned" ? pinnedPage : regularPage;
    const loadState = {
      nextCursor: page.nextCursor,
      loading: page.loadingMore,
      inFlight: paginationInFlight.current[kind],
      failedCursor: paginationFailedCursor.current[kind],
    };
    if (!canStartLibraryPageLoad(loadState)) return;

    const cursor = loadState.nextCursor;
    const generation = requestGeneration.current;
    const queryScope = selectionScope;
    const controller = new AbortController();
    const setPage = kind === "pinned" ? setPinnedPage : setRegularPage;
    paginationInFlight.current[kind] = true;
    paginationControllers.current[kind] = controller;
    setPage((current) => ({ ...current, loadingMore: true }));
    try {
      const result = await listLibrarySites({
        ...siteQuery,
        pinned: kind === "pinned",
        cursor,
      }, controller.signal);
      if (
        controller.signal.aborted
        || requestGeneration.current !== generation
        || activeSiteQueryScope.current !== queryScope
      ) return;
      paginationFailedCursor.current[kind] = null;
      setPage((current) => ({
        items: appendUniqueSites(current.items, result.items),
        nextCursor: result.nextCursor,
        matchedCount: result.aggregate.matchedCount,
        loadingMore: false,
      }));
    } catch (error) {
      if (
        isAbortError(error)
        || controller.signal.aborted
        || requestGeneration.current !== generation
        || activeSiteQueryScope.current !== queryScope
      ) return;
      paginationFailedCursor.current[kind] = cursor;
      setSitesError(errorMessage(error, "更多网站加载失败，请重试"));
    } finally {
      if (paginationControllers.current[kind] !== controller) return;
      paginationControllers.current[kind] = null;
      paginationInFlight.current[kind] = false;
      if (
        requestGeneration.current === generation
        && activeSiteQueryScope.current === queryScope
      ) {
        setPage((current) => ({ ...current, loadingMore: false }));
      }
    }
  }, [pinnedPage, regularPage, selectionScope, siteQuery]);

  const toggleAllMatchingSites = useCallback(async () => {
    if (allMatchingSelected) {
      clearSelectedSites();
      setNotice(null);
      return;
    }
    if (allMatchingSelectionBusy) return;

    selectionSnapshotController.current?.abort();
    const controller = new AbortController();
    const queryScope = selectionScope;
    selectionSnapshotController.current = controller;
    setAllMatchingSelectionBusy(true);
    setSitesError(null);
    try {
      const sites = await listLibrarySiteSelection({
        ...siteQuery,
        ...(pinnedOnly ? { pinned: true } : {}),
      }, controller.signal);
      if (
        controller.signal.aborted
        || activeSiteQueryScope.current !== queryScope
      ) return;
      if (sites.length === 0) {
        clearSelectedSites();
        setNotice("当前筛选结果已经发生变化，请刷新后重新选择");
        return;
      }
      selectAllMatchingSites(sites);
      setNotice(
        `已选择当前筛选命中的 ${sites.length} 个网站，删除时将按每批最多 ${MAX_LIBRARY_BULK_DELETE_SITES} 个处理`,
      );
    } catch (error) {
      if (isAbortError(error) || controller.signal.aborted) return;
      setSitesError(errorMessage(error, "全选当前筛选结果失败，请重试"));
    } finally {
      if (selectionSnapshotController.current === controller) {
        selectionSnapshotController.current = null;
        setAllMatchingSelectionBusy(false);
      }
    }
  }, [
    allMatchingSelected,
    allMatchingSelectionBusy,
    clearSelectedSites,
    pinnedOnly,
    selectAllMatchingSites,
    selectionScope,
    siteQuery,
  ]);

  const openDialog = (nextDialog: Exclude<DialogState, null>) => {
    setMutationError(null);
    setDialog(nextDialog);
  };

  const closeDialog = () => {
    if (mutationBusy) return;
    setDialog(null);
    setMutationError(null);
  };

  const beginBulkDelete = () => {
    if (selectedSites.length === 0) return;
    setMutationError(null);
    setBulkDeleteCompleted(0);
    setDialog({ kind: "bulk-delete", sites: selectedSites });
  };

  const handleCreate = async (input: LibrarySiteCreateInput) => {
    setMutationBusy(true);
    setMutationError(null);
    try {
      await createLibrarySite(input);
      setDialog(null);
      setNotice("网站已加入网址库");
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
      setNotice("网站已从网址库删除");
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

  const handleBulkDelete = async () => {
    if (dialog?.kind !== "bulk-delete") return;
    const deletingSites = dialog.sites;
    let confirmedDeleted = 0;
    setMutationBusy(true);
    setMutationError(null);
    setBulkDeleteCompleted(0);
    try {
      for (let offset = 0; offset < deletingSites.length; offset += MAX_LIBRARY_BULK_DELETE_SITES) {
        const batch = deletingSites.slice(offset, offset + MAX_LIBRARY_BULK_DELETE_SITES);
        const requestedIds = batch.map((site) => site.id);
        const result = await deleteLibrarySites(batch.map((site) => ({
          siteId: site.id,
          expectedVersion: site.version,
        })));
        if (
          result.deletedSiteIds.length !== requestedIds.length
          || result.deletedSiteIds.some((siteId) => !requestedIds.includes(siteId))
        ) {
          throw new Error("批量删除响应与所选网站不一致，请刷新后核对");
        }
        confirmedDeleted += batch.length;
        setBulkDeleteCompleted(confirmedDeleted);
      }
      setDialog(null);
      clearSelection();
      setNotice(`已从网址库删除 ${confirmedDeleted} 个网站`);
      refreshAfterMutation();
    } catch (error) {
      setDialog(null);
      clearSelection();
      if (isLibraryErrorCode(error, "bulk_delete_conflict")) {
        setNotice(confirmedDeleted === 0
          ? "所选网站已发生变化，本批未删除，已清空选择并刷新列表，请重新选择"
          : `已删除 ${confirmedDeleted} 个；下一批存在已变化的网站，剩余 ${deletingSites.length - confirmedDeleted} 个未处理，请刷新后重新选择`);
      } else {
        setNotice(confirmedDeleted === 0
          ? "删除请求结果未能确认，已停止后续批次并刷新，请核对后重新选择"
          : `已确认删除 ${confirmedDeleted} 个；后续请求结果未能确认，已停止剩余批次，请刷新后核对`);
      }
      refreshAfterMutation();
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
    clearSelection();
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
    selectionMode,
    selectionBusy: allMatchingSelectionBusy,
    selectedSiteIds,
    quickActionId,
    onToggleSelected: toggleSiteSelection,
    onEdit: (site: LibrarySite) => openDialog({ kind: "edit", site }),
    onDelete: (site: LibrarySite) => openDialog({ kind: "delete", site }),
    onTogglePinned: (site: LibrarySite) => void handleTogglePinned(site),
    reorderable: reorderable && !selectionMode,
    reorderBusy,
    onMove: (site: LibrarySite, to: "top" | "up" | "down" | "bottom") =>
      void handleMove(site, to),
    onDropBefore: (draggedId: string, beforeSiteId: string | null) =>
      void handleReorder([draggedId], beforeSiteId),
  };

  return {
    allLoadedSelected,
    allMatchingSelected,
    allMatchingSelectionBusy,
    analysisBackfillBusy,
    beginBulkDelete,
    bulkDeleteCompleted,
    categories,
    categoryId,
    closeDialog,
    clearSelection,
    collectionProps,
    dialog,
    direction,
    handleCreate,
    handleCreateTag,
    handleAnalysisBackfill,
    handleDelete,
    handleBulkDelete,
    handleSortChange,
    handleTaxonomyChanged,
    handleUpdate,
    hasActiveFilters,
    hasSites,
    loadMore,
    loadTaxonomies,
    loadedSiteCount,
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
    selectedSites,
    selectionMode,
    setCategoryId,
    setDirection,
    setNotice,
    setPinnedOnly,
    setSearchInput,
    setSearchQuery,
    setSelectionMode,
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
    toggleAllLoadedSites,
    toggleAllMatchingSites,
    viewMode,
  };
}
