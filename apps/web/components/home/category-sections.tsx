"use client";

import { ChevronDown } from "lucide-react";
import Link from "next/link";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";

import {
  ErrorNotice,
  errorText,
  isAbortError,
  prefersReducedMotion,
  siteHostname,
} from "@/components/home/home-shared";
import { SiteFavicon } from "@/components/site-favicon";
import { listLibraryCategories, listLibrarySites } from "@/lib/library-client";
import type {
  LibraryCategory,
  LibrarySite,
  LibrarySort,
} from "@/lib/library-contract";

// 首页分类分区 + 吸顶 Tabs（设计稿 1a 行 157–161、1c 行 292–343）。
// 分区标题（含站点数）随分类列表立刻渲染——这正好复现 1a 底部
// 「下一分区露出」的效果；卡片数据则等分区进入视口才拉取。
//
// 受控接口：activeId / onActiveChange 由首页保管（侧栏与 Tabs 共用同一份高亮），
// 滚动定位通过 useImperativeHandle 暴露 scrollToCategory，供侧栏点击时调用。
//
// 站点数据取用策略：选 IntersectionObserver 懒加载而不是 Promise.all 并发。
// 理由：分类数量不可控，首屏一次性并发会同时打出几十个请求，而其中大部分
// 分区用户根本不会滚到；按视口触发把请求摊平到滚动过程里，天然限流。
// rootMargin 预留 240px，让卡片在露出前就开始加载，视觉上无白屏。

type CategoriesState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; categories: LibraryCategory[] };

type SectionState =
  | { status: "pending" } // 尚未进入视口
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; sites: LibrarySite[] };

const SECTION_SITE_LIMIT = 8;

// 程序化滚动（点击 Tab / 侧栏）期间忽略观察回调的时长：
// 若不忽略，会形成「受控 activeId 变化 → 滚动 → 观察器命中中途分区 →
// onActiveChange 回写 → 高亮抖动」的回环。smooth 滚动通常在 300–600ms
// 内结束，取 1000ms 兜底长距离滚动；reduced-motion 下瞬时滚动同样适用。
const PROGRAMMATIC_SCROLL_IGNORE_MS = 1000;

// 后端没有「最近使用」维度，用更新时间近似（收录/编辑都会刷新 updated）。
// 这组选项同时覆盖 todolist 的「各种排序」基本诉求，值真实传入 listLibrarySites。
const SORT_OPTIONS: ReadonlyArray<{ value: LibrarySort; label: string }> = [
  { value: "updated", label: "按最近使用" },
  { value: "created", label: "按添加时间" },
  { value: "name", label: "按名称" },
];

const SORT_TITLE = "排序方式（最近使用按更新时间近似）";

function isLibrarySort(value: string): value is LibrarySort {
  return SORT_OPTIONS.some((option) => option.value === value);
}

export type CategorySectionsHandle = {
  scrollToCategory: (id: string | null) => void;
};

type CategorySectionsProps = {
  /** 当前高亮的分类；null 表示「全部」 */
  activeId?: string | null;
  /** 滚动经过分区或点击 Tab 时回写高亮 */
  onActiveChange?: (id: string | null) => void;
};

export const CategorySections = forwardRef<CategorySectionsHandle, CategorySectionsProps>(
  function CategorySections({ activeId = null, onActiveChange }, ref) {
    const [state, setState] = useState<CategoriesState>({ status: "loading" });
    const [reloadKey, setReloadKey] = useState(0);
    const [sort, setSort] = useState<LibrarySort>("updated");
    const blockRef = useRef<HTMLDivElement | null>(null);
    const sectionRefs = useRef(new Map<string, HTMLElement>());
    // 程序化滚动的「静默截止时刻」；观察回调在此之前不回写高亮
    const ignoreObserverUntilRef = useRef(0);
    // 回调收进 ref：避免父层每次渲染换引用导致观察器反复重建。
    // 在 effect 里同步（而非渲染期赋值）以符合 react-hooks/refs；
    // 本 effect 声明在观察器 effect 之前，每次提交都先于它执行。
    const onActiveChangeRef = useRef(onActiveChange);
    useEffect(() => {
      onActiveChangeRef.current = onActiveChange;
    }, [onActiveChange]);

    useEffect(() => {
      const controller = new AbortController();
      const load = async () => {
        setState({ status: "loading" });
        try {
          const categories = await listLibraryCategories(controller.signal);
          if (controller.signal.aborted) return;
          setState({ status: "ready", categories });
        } catch (error) {
          if (isAbortError(error) || controller.signal.aborted) return;
          setState({ status: "error", message: errorText(error, "分类加载失败") });
        }
      };
      void load();
      return () => controller.abort();
    }, [reloadKey]);

    const registerSection = useCallback((id: string, element: HTMLElement | null) => {
      const map = sectionRefs.current;
      if (element) {
        map.set(id, element);
      } else {
        map.delete(id);
      }
    }, []);

    // 滚动时高亮当前分区对应的 Tab：取「可见分区中最靠近顶部」的那个。
    // rootMargin 上缘扣掉 Header + 吸顶 Tabs 的高度，下缘收 55%，
    // 让分区标题滚进上半屏时才切换，避免 Tab 抖动。
    useEffect(() => {
      if (state.status !== "ready" || state.categories.length === 0) return;
      if (typeof IntersectionObserver === "undefined") return;

      const sections = sectionRefs.current;
      const visibleIds = new Set<string>();
      const observer = new IntersectionObserver(
        (entries) => {
          // 可见集合始终维护（程序化滚动结束后要凭它恢复判断），
          // 但静默期内不回写高亮，切断「滚动 → 回写 → 再滚动」的回环。
          for (const entry of entries) {
            const id = entry.target.getAttribute("data-category-id");
            if (!id) continue;
            if (entry.isIntersecting) {
              visibleIds.add(id);
            } else {
              visibleIds.delete(id);
            }
          }
          if (Date.now() < ignoreObserverUntilRef.current) return;
          let topId: string | null = null;
          let topOffset = Number.POSITIVE_INFINITY;
          for (const id of visibleIds) {
            const element = sections.get(id);
            if (!element) continue;
            const top = element.getBoundingClientRect().top;
            if (top < topOffset) {
              topOffset = top;
              topId = id;
            }
          }
          if (topId) onActiveChangeRef.current?.(topId);
        },
        { rootMargin: "-128px 0px -55% 0px" },
      );
      for (const element of sections.values()) observer.observe(element);
      return () => observer.disconnect();
    }, [state]);

    const scrollToCategory = useCallback((id: string | null) => {
      // 先立起静默标记再滚动：scrollIntoView 是同步触发观察队列的
      ignoreObserverUntilRef.current = Date.now() + PROGRAMMATIC_SCROLL_IGNORE_MS;
      const target = id ? sectionRefs.current.get(id) : blockRef.current;
      target?.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "start",
      });
    }, []);

    useImperativeHandle(ref, () => ({ scrollToCategory }), [scrollToCategory]);

    // Tab 点击：高亮交给受控方回写，滚动仍由本组件执行
    const handleTabSelect = useCallback(
      (id: string | null) => {
        onActiveChangeRef.current?.(id);
        scrollToCategory(id);
      },
      [scrollToCategory],
    );

    return (
      <div className="home-category-block" ref={blockRef}>
        {state.status === "loading" && (
          <div aria-hidden="true">
            <div className="home-category-tabs">
              {Array.from({ length: 5 }, (_, index) => (
                <span
                  className="home-skeleton-block"
                  style={{ width: index === 0 ? 64 : 88, height: 33, borderRadius: "var(--radius-md)" }}
                  key={index}
                />
              ))}
            </div>
            <div className="home-category-sections">
              <SectionSkeleton count={SECTION_SITE_LIMIT} withHeading />
            </div>
          </div>
        )}

        {state.status === "error" && (
          <ErrorNotice
            message={state.message}
            onRetry={() => setReloadKey((key) => key + 1)}
          />
        )}

        {state.status === "ready" && state.categories.length === 0 && (
          <p className="home-empty">还没有分类，收录网站后这里会按分类展示。</p>
        )}

        {state.status === "ready" && state.categories.length > 0 && (
          <>
            <nav className="home-category-tabs" aria-label="分类导航">
              <button
                type="button"
                className="home-category-tab"
                data-active={activeId === null}
                onClick={() => handleTabSelect(null)}
              >
                全部
              </button>
              {state.categories.map((category) => (
                <button
                  key={category.id}
                  type="button"
                  className="home-category-tab"
                  data-active={activeId === category.id}
                  onClick={() => handleTabSelect(category.id)}
                >
                  {category.name}
                </button>
              ))}
              <span className="home-section-spacer" />
              <SortSelect sort={sort} onSortChange={setSort} />
            </nav>
            <div className="home-category-sections">
              {state.categories.map((category) => (
                <CategorySection
                  key={category.id}
                  category={category}
                  sort={sort}
                  onSortChange={setSort}
                  onRegister={registerSection}
                />
              ))}
            </div>
          </>
        )}
      </div>
    );
  },
);

type SortSelectProps = {
  sort: LibrarySort;
  onSortChange: (sort: LibrarySort) => void;
  /** plain：分区标题行内的无边框轻量形态（1c 行 319） */
  variant?: "boxed" | "plain";
};

/* 排序控件（1c 行 308 / 319）：原生 select 保证键盘可操作，
   外观用 appearance:none + 覆盖式 chevron 对齐设计稿的按钮形态。 */
function SortSelect({ sort, onSortChange, variant = "boxed" }: Readonly<SortSelectProps>) {
  return (
    <span
      className={variant === "plain" ? "home-sort home-sort--plain" : "home-sort"}
      title={SORT_TITLE}
    >
      <select
        className="home-sort-select"
        aria-label="排序方式"
        value={sort}
        onChange={(event) => {
          const next = event.target.value;
          if (isLibrarySort(next)) onSortChange(next);
        }}
      >
        {SORT_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <ChevronDown className="home-sort-chevron" aria-hidden="true" />
    </span>
  );
}

type CategorySectionProps = {
  category: LibraryCategory;
  sort: LibrarySort;
  onSortChange: (sort: LibrarySort) => void;
  onRegister: (id: string, element: HTMLElement | null) => void;
};

function CategorySection({
  category,
  sort,
  onSortChange,
  onRegister,
}: Readonly<CategorySectionProps>) {
  // 没有 IntersectionObserver 的运行时直接视为可见：宁可多请求也不能不显示
  const [visible, setVisible] = useState(
    () => typeof IntersectionObserver === "undefined",
  );
  const [reloadKey, setReloadKey] = useState(0);
  const [state, setState] = useState<SectionState>({ status: "pending" });
  const sectionRef = useRef<HTMLElement | null>(null);

  // 空分类不必发请求：siteCount 已由分类接口给出
  const hasSites = category.siteCount > 0;

  useEffect(() => {
    if (!hasSites || visible) return;
    const element = sectionRef.current;
    if (!element) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "240px 0px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [hasSites, visible]);

  useEffect(() => {
    if (!hasSites || !visible) return;
    const controller = new AbortController();
    const load = async () => {
      setState({ status: "loading" });
      try {
        const page = await listLibrarySites(
          {
            categoryId: category.id,
            limit: SECTION_SITE_LIMIT,
            sort,
            // 名称排序按 A→Z 才符合直觉，时间类排序保持最新在前
            direction: sort === "name" ? "asc" : "desc",
          },
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setState({ status: "ready", sites: page.items });
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        setState({ status: "error", message: errorText(error, "分类内容加载失败") });
      }
    };
    void load();
    return () => controller.abort();
  }, [hasSites, visible, reloadKey, category.id, sort]);

  const skeletonCount = Math.min(category.siteCount, SECTION_SITE_LIMIT);

  return (
    <section
      className="home-category-section"
      data-category-id={category.id}
      aria-label={category.name}
      ref={(element) => {
        sectionRef.current = element;
        onRegister(category.id, element);
      }}
    >
      <div className="home-category-heading">
        <span className="home-category-mark" aria-hidden="true" />
        <h2 className="home-category-title">{category.name}</h2>
        <span className="home-category-count">{category.siteCount} 个网站</span>
        <span className="home-section-spacer" />
        <SortSelect sort={sort} onSortChange={onSortChange} variant="plain" />
        <Link
          className="home-section-action"
          href={`/library?category=${encodeURIComponent(category.id)}`}
        >
          查看更多 →
        </Link>
      </div>

      {!hasSites && <p className="home-empty">该分类下还没有网站。</p>}

      {hasSites && (state.status === "pending" || state.status === "loading") && (
        <SectionSkeleton count={skeletonCount} />
      )}

      {hasSites && state.status === "error" && (
        <ErrorNotice
          message={state.message}
          onRetry={() => setReloadKey((key) => key + 1)}
        />
      )}

      {hasSites && state.status === "ready" && state.sites.length === 0 && (
        <p className="home-empty">该分类下还没有网站。</p>
      )}

      {hasSites && state.status === "ready" && state.sites.length > 0 && (
        <div className="home-card-grid">
          {state.sites.map((site) => (
            <a
              key={site.id}
              className="home-site-card"
              href={site.originalUrl}
              target="_blank"
              rel="noreferrer noopener"
              title={site.name}
            >
              <span className="home-site-card-head">
                <SiteFavicon url={site.faviconUrl} name={site.name} size={22} />
                <span className="home-site-name">{site.name}</span>
                {site.tags[0] && <span className="home-site-tag">{site.tags[0].name}</span>}
              </span>
              <span className="home-site-desc">{site.description?.trim() ?? ""}</span>
              <span className="home-site-host">{siteHostname(site.originalUrl)}</span>
            </a>
          ))}
        </div>
      )}
    </section>
  );
}

type SectionSkeletonProps = {
  count: number;
  withHeading?: boolean;
};

/** 骨架卡与真实卡同类名同 min-height，保证懒加载完成时零布局位移 */
function SectionSkeleton({ count, withHeading = false }: Readonly<SectionSkeletonProps>) {
  return (
    <div aria-hidden="true">
      {withHeading && (
        <div className="home-category-heading">
          <span className="home-category-mark" />
          <span className="home-skeleton-bar" style={{ width: 72, height: 15 }} />
          <span className="home-skeleton-bar" style={{ width: 56, height: 12 }} />
        </div>
      )}
      <div className="home-card-grid">
        {Array.from({ length: Math.max(count, 1) }, (_, index) => (
          <div className="home-site-card home-skeleton-card" key={index}>
            <span className="home-site-card-head">
              <span className="home-skeleton-block" style={{ width: 22, height: 22 }} />
              <span className="home-skeleton-bar" style={{ width: "52%", height: 12 }} />
            </span>
            <span
              className="home-skeleton-bar"
              style={{ width: "86%", height: 11, marginTop: 10 }}
            />
            <span
              className="home-skeleton-bar"
              style={{ width: "40%", height: 10, marginTop: 8 }}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
