"use client";

import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  GalleryMasonry,
  GallerySkeleton,
  useGalleryGrid,
  type GalleryEntrance,
} from "@/components/discover/gallery-masonry";
import { RediscoverNotice } from "@/components/discover/rediscover-parts";
import { useSiteActions } from "@/components/discover/use-site-actions";
import { LibraryAutoLoad } from "@/components/library/library-auto-load";
import { useToast } from "@/components/ui/toast";
import { ThemedSelect, type ThemedSelectOption } from "@/components/ui/themed-select";
import { listLibrarySites } from "@/lib/library-client";
import type {
  LibraryOverview,
  LibrarySite,
  LibrarySiteKind,
  LibrarySiteState,
} from "@/lib/library-contract";
import { subscribeSiteUpdated } from "@/lib/library-events";

// 发现页 B 区“全部收藏”：类型标签 + 状态分段 + 排序，封面瀑布流，静默无限加载。
// 切换筛选时整页重置，并用请求代号丢弃过期响应（快速连点标签不会串结果）。

const PAGE_SIZE = 24;
/** 入场动效结束后清掉标记：列数变化导致卡片重挂时不再重播。 */
const ENTRANCE_CLEAR_MS = 1_400;

type KindFilter = LibrarySiteKind | "all";
type StateFilter = Extract<LibrarySiteState, "unopened" | "starred"> | "all";
type SortFilter = "created" | "opened";

const KIND_TABS: ReadonlyArray<{ value: KindFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "repo", label: "仓库" },
  { value: "article", label: "帖子与文章" },
  { value: "website", label: "工具与网站" },
  { value: "video", label: "视频" },
];

const STATE_TABS: ReadonlyArray<{ value: StateFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "unopened", label: "未打开" },
  { value: "starred", label: "星标" },
];

const SORT_OPTIONS: readonly ThemedSelectOption<SortFilter>[] = [
  { value: "created", label: "最近收藏" },
  { value: "opened", label: "最近打开" },
];

export const GALLERY_TITLE_ID = "discover-gallery-title";

type ListState = {
  status: "loading" | "ready" | "error";
  items: LibrarySite[];
  nextCursor: string | null;
  error: string | null;
  loadingMore: boolean;
  moreError: string | null;
  now: number;
  entrance: GalleryEntrance;
};

const INITIAL_LIST: ListState = {
  status: "loading",
  items: [],
  nextCursor: null,
  error: null,
  loadingMore: false,
  moreError: null,
  now: 0,
  entrance: {},
};

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function formatCount(value: number): string {
  return value.toLocaleString("zh-CN");
}

function emptyMessage(kind: KindFilter, state: StateFilter): string {
  const label = KIND_TABS.find((tab) => tab.value === kind)?.label ?? "";
  if (state === "unopened") {
    return kind === "all" ? "还没有未打开的收藏，收进来的都看过了。" : `「${label}」里没有未打开的收藏。`;
  }
  if (state === "starred") {
    return kind === "all"
      ? "还没有星标收藏。悬停卡片点星标，把宝藏留在这里。"
      : `「${label}」里还没有星标收藏。`;
  }
  return kind === "all" ? "还没有收藏。" : `还没有「${label}」类的收藏。`;
}

type GallerySectionProps = {
  overview: LibraryOverview | null;
  /** 收藏集合变化（快速收藏）时递增，触发从第一页重新加载。 */
  revision: number;
};

export function GallerySection({ overview, revision }: Readonly<GallerySectionProps>) {
  const [kind, setKind] = useState<KindFilter>("all");
  const [state, setState] = useState<StateFilter>("all");
  const [sort, setSort] = useState<SortFilter>("created");
  const [list, setList] = useState<ListState>(INITIAL_LIST);
  const [reloadKey, setReloadKey] = useState(0);
  const generationRef = useRef(0);
  const entranceTimerRef = useRef<number | null>(null);
  const { ref: gridRef, grid } = useGalleryGrid();
  const { openSite, changeSiteState } = useSiteActions();
  const toast = useToast();

  const query = useCallback(
    (cursor?: string) => ({
      ...(kind === "all" ? {} : { kind }),
      ...(state === "all" ? {} : { state }),
      sort,
      direction: "desc" as const,
      limit: PAGE_SIZE,
      ...(cursor ? { cursor } : {}),
    }),
    [kind, sort, state],
  );

  const scheduleEntranceClear = useCallback(() => {
    if (entranceTimerRef.current !== null) window.clearTimeout(entranceTimerRef.current);
    entranceTimerRef.current = window.setTimeout(() => {
      entranceTimerRef.current = null;
      setList((current) => ({ ...current, entrance: {} }));
    }, ENTRANCE_CLEAR_MS);
  }, []);

  useEffect(() => () => {
    if (entranceTimerRef.current !== null) window.clearTimeout(entranceTimerRef.current);
  }, []);

  // 第一页：筛选、排序或收藏集合变化时从头加载
  useEffect(() => {
    const controller = new AbortController();
    generationRef.current += 1;
    const generation = generationRef.current;
    const run = async () => {
      setList(INITIAL_LIST);
      try {
        const page = await listLibrarySites(query(), controller.signal);
        if (controller.signal.aborted || generation !== generationRef.current) return;
        setList({
          status: "ready",
          items: page.items,
          nextCursor: page.nextCursor,
          error: null,
          loadingMore: false,
          moreError: null,
          now: Date.now(),
          entrance: Object.fromEntries(page.items.map((site, index) => [site.id, index])),
        });
        scheduleEntranceClear();
      } catch (error) {
        if (controller.signal.aborted || isAbort(error) || generation !== generationRef.current) return;
        setList({ ...INITIAL_LIST, status: "error", error: `收藏没有读出来：${errorMessage(error, "请稍后重试")}` });
      }
    };
    void run();
    return () => controller.abort();
  }, [query, reloadKey, revision, scheduleEntranceClear]);

  const loadMore = useCallback(async () => {
    const cursor = list.nextCursor;
    if (list.status !== "ready" || list.loadingMore || !cursor) return;
    const generation = generationRef.current;
    setList((current) => ({ ...current, loadingMore: true, moreError: null }));
    try {
      const page = await listLibrarySites(query(cursor));
      if (generation !== generationRef.current) return;
      setList((current) => {
        const known = new Set(current.items.map((site) => site.id));
        const fresh = page.items.filter((site) => !known.has(site.id));
        return {
          ...current,
          items: [...current.items, ...fresh],
          nextCursor: page.nextCursor,
          loadingMore: false,
          entrance: { ...current.entrance, ...Object.fromEntries(fresh.map((site) => [site.id, "fade" as const])) },
        };
      });
      scheduleEntranceClear();
    } catch (error) {
      if (generation !== generationRef.current) return;
      setList((current) => ({
        ...current,
        loadingMore: false,
        moreError: `更多收藏没有读出来：${errorMessage(error, "请稍后重试")}`,
      }));
    }
  }, [list.loadingMore, list.nextCursor, list.status, query, scheduleEntranceClear]);

  // 主卡与卡片上的操作广播到这里，就地替换，不重新取数（也不改变卡片位置）
  useEffect(
    () =>
      subscribeSiteUpdated((site) => {
        setList((current) =>
          current.items.some((item) => item.id === site.id)
            ? { ...current, items: current.items.map((item) => (item.id === site.id ? site : item)) }
            : current,
        );
      }),
    [],
  );

  const handleOpen = useCallback((site: LibrarySite) => {
    openSite(site);
  }, [openSite]);

  const handleToggleStar = useCallback(
    (site: LibrarySite) => {
      changeSiteState(site, { starred: !site.starred }).catch(() => {
        toast.show({ message: "星标没有保存，请稍后重试", tone: "danger" });
      });
    },
    [changeSiteState, toast],
  );

  const handleMarkSeen = useCallback(
    (site: LibrarySite) => {
      changeSiteState(site, { seen: true })
        .then((saved) => {
          toast.show({
            message: "已标记为已看",
            action: {
              label: "撤销",
              onAction: () => {
                changeSiteState(saved, { seen: false }).catch(() => {
                  toast.show({ message: "撤销没有成功，请稍后重试", tone: "danger" });
                });
              },
            },
          });
        })
        .catch(() => {
          toast.show({ message: "没有标记成功，请稍后重试", tone: "danger" });
        });
    },
    [changeSiteState, toast],
  );

  const filtersActive = kind !== "all" || state !== "all";
  const kindCount = (value: KindFilter): number | null => {
    if (!overview) return null;
    return value === "all" ? overview.total : overview.byKind[value];
  };

  return (
    <section className="discover-section discover-gallery" aria-labelledby={GALLERY_TITLE_ID}>
      <div className="discover-gallery-head">
        <h2 className="discover-gallery-title" id={GALLERY_TITLE_ID}>
          全部收藏
          {overview && <span className="discover-gallery-total">{formatCount(overview.total)}</span>}
        </h2>
        <div className="discover-kind-tabs" role="group" aria-label="按类型筛选">
          {KIND_TABS.map((tab) => {
            const count = kindCount(tab.value);
            const active = kind === tab.value;
            return (
              <button
                key={tab.value}
                className="discover-kind-tab"
                type="button"
                aria-pressed={active}
                data-active={active || undefined}
                onClick={() => setKind(tab.value)}
              >
                {tab.label}
                {count !== null && <span className="discover-kind-count">{formatCount(count)}</span>}
              </button>
            );
          })}
        </div>
        <span className="discover-gallery-head-spacer" aria-hidden="true" />
        <div className="discover-gallery-controls">
          <div className="discover-segmented" role="group" aria-label="按状态筛选">
            {STATE_TABS.map((tab) => (
              <button
                key={tab.value}
                className="discover-segment"
                type="button"
                aria-pressed={state === tab.value}
                data-active={state === tab.value || undefined}
                onClick={() => setState(tab.value)}
              >
                {tab.label}
              </button>
            ))}
          </div>
          <ThemedSelect
            className="discover-sort"
            value={sort}
            options={SORT_OPTIONS}
            onValueChange={setSort}
            ariaLabel="排序方式"
            variant="plain"
            align="end"
          />
        </div>
      </div>

      <div className="discover-gallery-body" ref={gridRef} aria-busy={list.status === "loading"}>
        {list.status === "loading" && grid && <GallerySkeleton count={grid.count} />}
        {list.status === "loading" && <span className="sr-only">正在读取收藏</span>}

        {list.status === "error" && (
          <RediscoverNotice
            tone="error"
            message={list.error ?? "收藏没有读出来，请稍后重试"}
            actionLabel="重试"
            onAction={() => setReloadKey((value) => value + 1)}
          />
        )}

        {list.status === "ready" && list.items.length === 0 && (
          <div className="discover-notice" data-tone="empty">
            <p className="discover-notice-text">{emptyMessage(kind, state)}</p>
            {filtersActive && (
              <button
                className="discover-text-button"
                type="button"
                onClick={() => {
                  setKind("all");
                  setState("all");
                }}
              >
                看全部收藏
              </button>
            )}
          </div>
        )}

        {list.status === "ready" && list.items.length > 0 && grid && (
          <GalleryMasonry
            grid={grid}
            items={list.items}
            now={list.now}
            entrance={list.entrance}
            onOpen={handleOpen}
            onToggleStar={handleToggleStar}
            onMarkSeen={handleMarkSeen}
          />
        )}

        {list.status === "ready" && list.moreError ? (
          <div className="discover-gallery-more-error" role="alert">
            <span>{list.moreError}</span>
            <button className="discover-text-button" type="button" onClick={() => void loadMore()}>
              <RefreshCw aria-hidden="true" />
              重试
            </button>
          </div>
        ) : (
          list.status === "ready" && (
            <LibraryAutoLoad
              hasMore={list.nextCursor !== null}
              loading={list.loadingMore}
              loadingLabel="正在加载更多收藏"
              fallbackLabel="加载更多收藏"
              onLoadMore={loadMore}
            />
          )
        )}
      </div>
    </section>
  );
}
