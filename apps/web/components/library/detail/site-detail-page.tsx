"use client";

import {
  AlarmClock,
  ArrowLeft,
  ArrowUpRight,
  Box,
  Check,
  ChevronRight,
  CircleAlert,
  Ellipsis,
  Eye,
  Pencil,
  Pin,
  RefreshCw,
  Star,
  Trash2,
  WandSparkles,
  X,
  ZoomIn,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { ResourceCover } from "@/components/discover/resource-cover";
import { useShortcutTarget } from "@/components/keyboard/shortcut-target";
import { LibraryDialog } from "@/components/library/library-dialog";
import { SiteForm } from "@/components/library/site-form";
import { useSiteQuickActions } from "@/components/library/use-site-quick-actions";
import { Spinner } from "@/components/react-bits/spinner";
import { SiteFavicon } from "@/components/site-favicon";
import {
  hasRefreshableSiteAnalysis,
  useBoundedAnalysisRefresh,
} from "@/lib/analysis-refresh";
import {
  analyzeLibrarySite,
  createLibraryTagResolvingConflict,
  deleteLibrarySite,
  getLibrarySite,
  LibraryApiError,
  listLibraryCategories,
  listLibraryTags,
  updateLibrarySite,
} from "@/lib/library-client";
import type {
  LibraryCategory,
  LibrarySite,
  LibrarySiteUpdateInput,
  LibraryTag,
} from "@/lib/library-contract";
import { subscribeSiteUpdated } from "@/lib/library-events";
import { formatRelativeTime } from "@/lib/relative-time";
import { coverModel, isSafePreviewUrl, resourceKindLabel, resourceTitle } from "@/lib/resource-cover";
import {
  siteAnalysisProgressRows,
  type SiteAnalysisDisplayPhase,
} from "@/lib/site-analysis-progress";
import { isUnopened, withSiteUsage } from "@/lib/site-usage";

import { AgentHelpCard } from "./agent-help-card";
import { JoinSpaceDialog } from "./join-space-dialog";
import { formatDay, hostOf, SOURCE_LABELS } from "./meta";
import { RelatedSitesCard } from "./related-sites-card";
import { RepoPanel } from "./repo-panel";
import { TaxonomyEditor } from "./taxonomy-editor";
import { UsageCard } from "./usage-card";
import { useDismissable } from "./use-dismissable";

/* 收藏详情页（Q46，2026-10 D5 / D6）。
   顶部是复用发现页的大封面；标题下一行是“类型 · 域名 · 收藏时间 · 来源”，再下是操作行
   （打开 / 星标 / 已看 / 推迟 / 编辑 / 更多）。星标、已看、推迟乐观更新并给 5 秒撤销；
   删除收进“更多”菜单并二次确认。分类与标签就地编辑，仓库面板只在有 GitHub 数据时出现。
   编辑与删除都带 expectedVersion 做乐观并发；版本冲突时载入最新版本让用户再确认。 */

type SiteDetailPageProps = {
  siteId: string;
};

type LoadState =
  | { status: "loading" }
  | { status: "ready"; site: LibrarySite }
  | { status: "not-found"; message: string }
  | { status: "error"; message: string };

type StatusState = Exclude<LoadState, { status: "ready" }>;

type DialogKind = "edit" | "delete" | "space" | null;

type NoticeState = {
  tone: "success" | "warning" | "error";
  message: string;
};

type TaxonomyState =
  | { status: "loading" }
  | { status: "ready"; categories: LibraryCategory[]; tags: LibraryTag[] }
  | { status: "error"; message: string };

/** 与旧版一致的 siteId 防御：空串、超长、控制字符都视为非法链接 */
function validSiteId(value: string): string | null {
  const normalized = value.trim();
  if (!normalized || normalized.length > 256 || /[\u0000-\u001f\u007f]/u.test(normalized)) return null;
  return normalized;
}

function isLibraryErrorCode(error: unknown, code: string): error is LibraryApiError {
  return error instanceof LibraryApiError && error.code === code;
}

function isLibraryNotFound(error: unknown): error is LibraryApiError {
  return error instanceof LibraryApiError && (error.code === "not_found" || error.status === 404);
}

/** 同一条收藏的新快照：内容版本更新时整条替换，否则只接收使用信号（打开 / 星标 / 已看 / 推迟）。 */
function mergeSite(current: LibrarySite, incoming: LibrarySite): LibrarySite {
  if (current.id !== incoming.id) return current;
  return incoming.version >= current.version ? incoming : withSiteUsage(current, incoming);
}

function SiteAnalysisProgress({
  phase,
  elapsedSeconds,
}: Readonly<{
  phase: SiteAnalysisDisplayPhase;
  elapsedSeconds: number;
}>) {
  const rows = siteAnalysisProgressRows(phase);
  const current = rows.find((row) => row.state === "current") ?? rows[0];
  return (
    <div className="sd-analysis-progress">
      <div
        className="sd-analysis-progress-head"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        <span>
          <Spinner size={13} />
          {current.label}
        </span>
        <time>{elapsedSeconds} 秒</time>
      </div>
      <ol className="sd-analysis-progress-steps">
        {rows.map((row) => (
          <li key={row.phase} data-state={row.state}>
            <span className="sd-analysis-progress-icon" aria-hidden="true">
              {row.state === "done" ? (
                <Check size={11} />
              ) : row.state === "current" ? (
                <Spinner size={11} />
              ) : (
                <span />
              )}
            </span>
            {row.label}
          </li>
        ))}
      </ol>
    </div>
  );
}

function DetailStatus({ state, onRetry }: Readonly<{ state: StatusState; onRetry: () => void }>) {
  const loading = state.status === "loading";

  return (
    <main className="sd-page">
      <Link href="/library" className="sd-back-link">
        <ArrowLeft size={16} aria-hidden="true" />
        返回收藏库
      </Link>
      {loading ? (
        <div className="sd-skeleton" aria-busy="true" aria-label="正在读取收藏">
          <span className="sd-skeleton-block sd-skeleton-cover" />
          <span className="sd-skeleton-block sd-skeleton-title" />
          <span className="sd-skeleton-block sd-skeleton-line" />
        </div>
      ) : (
        <section className="sd-status" aria-live="polite">
          <span className="sd-status-icon" aria-hidden="true">
            <CircleAlert size={16} />
          </span>
          <h1 className="sd-status-title">
            {state.status === "not-found" ? "收藏不存在" : "暂时无法读取收藏"}
          </h1>
          <p className="sd-status-text">{state.message}</p>
          {state.status === "error" && (
            <button type="button" className="sd-btn sd-btn-primary sd-status-retry" onClick={onRetry}>
              <RefreshCw size={16} aria-hidden="true" />
              重新加载
            </button>
          )}
        </section>
      )}
    </main>
  );
}

type MoreMenuProps = {
  analyzing: boolean;
  analyzeLabel: string;
  onAnalyze: () => void;
  onJoinSpace: () => void;
  onDelete: () => void;
};

/** “更多”菜单：低频操作与唯一的破坏性操作（删除，二次确认）。 */
function MoreMenu({ analyzing, analyzeLabel, onAnalyze, onJoinSpace, onDelete }: Readonly<MoreMenuProps>) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  useDismissable(open, rootRef, triggerRef, () => setOpen(false));

  useEffect(() => {
    if (open) menuRef.current?.querySelector<HTMLElement>("[role='menuitem']")?.focus();
  }, [open]);

  const select = (action: () => void) => {
    setOpen(false);
    action();
  };

  return (
    <div className="sd-picker" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="sd-btn sd-btn-secondary sd-btn-icon"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="更多操作"
        title="更多操作"
        onClick={() => setOpen((current) => !current)}
      >
        <Ellipsis size={16} aria-hidden="true" />
      </button>
      {open && (
        <div
          ref={menuRef}
          className="sd-popover sd-popover-menu"
          role="menu"
          aria-label="更多操作"
          data-shortcut-block
          onKeyDown={(event) => {
            if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
            event.preventDefault();
            const items = Array.from(
              event.currentTarget.querySelectorAll<HTMLElement>("[role='menuitem']:not(:disabled)"),
            );
            const index = items.indexOf(document.activeElement as HTMLElement);
            const next = event.key === "ArrowDown" ? index + 1 : index - 1;
            items[(next + items.length) % items.length]?.focus();
          }}
        >
          <button type="button" role="menuitem" className="sd-popover-option" disabled={analyzing} onClick={() => select(onAnalyze)}>
            <WandSparkles size={14} aria-hidden="true" />
            {analyzeLabel}
          </button>
          <button type="button" role="menuitem" className="sd-popover-option" onClick={() => select(onJoinSpace)}>
            <Box size={14} aria-hidden="true" />
            加入 Space
          </button>
          <div className="sd-popover-divider" aria-hidden="true" />
          <button
            type="button"
            role="menuitem"
            className="sd-popover-option sd-popover-danger"
            onClick={() => select(onDelete)}
          >
            <Trash2 size={14} aria-hidden="true" />
            删除收藏…
          </button>
        </div>
      )}
    </div>
  );
}

export function SiteDetailPage({ siteId }: Readonly<SiteDetailPageProps>) {
  const router = useRouter();
  const normalizedSiteId = validSiteId(siteId);
  const quickActions = useSiteQuickActions();

  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [loadAttempt, setLoadAttempt] = useState(0);
  // 相对时间的基准：取数时记录，避免渲染期读时钟
  const [now, setNow] = useState(0);

  const [dialog, setDialog] = useState<DialogKind>(null);
  const [busy, setBusy] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [notice, setNotice] = useState<NoticeState | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisStartedAt, setAnalysisStartedAt] = useState<number | null>(null);
  const [analysisElapsedSeconds, setAnalysisElapsedSeconds] = useState(0);
  const analysisGenerationRef = useRef(0);
  const [failedPreviewUrl, setFailedPreviewUrl] = useState<string | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const previewTriggerRef = useRef<HTMLButtonElement>(null);
  const previewCloseRef = useRef<HTMLButtonElement>(null);

  // 编辑表单需要完整的分类/标签列表；每次打开编辑弹层都重新拉取，保证是最新的
  const [taxonomy, setTaxonomy] = useState<TaxonomyState>({ status: "loading" });
  const [taxonomyAttempt, setTaxonomyAttempt] = useState(0);
  // 每次打开「加入 Space」弹层递增，换 key 重挂载弹层以拉取最新 Space 列表
  const [spaceDialogSeq, setSpaceDialogSeq] = useState(0);

  const retry = useCallback(() => {
    setState({ status: "loading" });
    setLoadAttempt((attempt) => attempt + 1);
  }, []);

  useEffect(() => {
    if (!normalizedSiteId) return;
    const controller = new AbortController();

    void getLibrarySite(normalizedSiteId, controller.signal)
      .then((site) => {
        setState({ status: "ready", site });
        setNow(Date.now());
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (isLibraryNotFound(error)) {
          setState({ status: "not-found", message: "这条收藏可能已被删除，或不属于当前登录账号。" });
          return;
        }
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "收藏库服务暂时不可用，请稍后重试。",
        });
      });

    return () => controller.abort();
  }, [loadAttempt, normalizedSiteId]);

  // 星标 / 已看 / 推迟（含快捷键与撤销）都经 announceSiteUpdated 广播，这里就地合并
  useEffect(
    () =>
      subscribeSiteUpdated((incoming) => {
        setState((current) =>
          current.status === "ready" && current.site.id === incoming.id
            ? { status: "ready", site: mergeSite(current.site, incoming) }
            : current,
        );
        setNow(Date.now());
      }),
    [],
  );

  const analysisRefreshEnabled = !analyzing && state.status === "ready"
    ? hasRefreshableSiteAnalysis([state.site])
    : false;
  const refreshAnalysis = useCallback(async () => {
    if (!normalizedSiteId) return;
    try {
      const site = await getLibrarySite(normalizedSiteId);
      setState((current) => (
        current.status === "ready"
        && current.site.id === site.id
        && site.version >= current.site.version
          ? { status: "ready", site }
          : current
      ));
    } catch {
      // A background status refresh must not replace a usable detail page with
      // a transient network error. The explicit retry path remains available.
    }
  }, [normalizedSiteId]);
  useBoundedAnalysisRefresh({
    scope: normalizedSiteId ?? "invalid-site",
    enabled: analysisRefreshEnabled,
    refresh: refreshAnalysis,
  });

  useEffect(() => {
    if (!analyzing || analysisStartedAt === null) return;
    const update = () => {
      setAnalysisElapsedSeconds(Math.max(0, Math.floor((Date.now() - analysisStartedAt) / 1_000)));
    };
    update();
    const timer = window.setInterval(update, 1_000);
    return () => window.clearInterval(timer);
  }, [analysisStartedAt, analyzing]);

  useEffect(() => {
    if (!analyzing || !normalizedSiteId) return;
    const controller = new AbortController();
    const generation = analysisGenerationRef.current;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const site = await getLibrarySite(normalizedSiteId, controller.signal);
        if (controller.signal.aborted || analysisGenerationRef.current !== generation) return;
        setState((current) => (
          current.status === "ready"
          && current.site.id === site.id
          && site.version >= current.site.version
            ? { status: "ready", site }
            : current
        ));
      } catch {
        // The result POST remains authoritative. A transient progress read
        // should neither stop analysis nor replace the usable detail page.
      }
      if (!controller.signal.aborted && analysisGenerationRef.current === generation) {
        timer = window.setTimeout(poll, 800);
      }
    };

    timer = window.setTimeout(poll, 200);
    return () => {
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [analyzing, normalizedSiteId]);

  // loading 态在打开弹层/点击重试的事件里同步设置，效果内只负责发请求
  useEffect(() => {
    if (dialog !== "edit") return;
    const controller = new AbortController();

    void Promise.all([listLibraryCategories(controller.signal), listLibraryTags(controller.signal)])
      .then(([categories, tags]) => setTaxonomy({ status: "ready", categories, tags }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setTaxonomy({
          status: "error",
          message: error instanceof Error ? error.message : "分类与标签加载失败，请稍后重试。",
        });
      });

    return () => controller.abort();
  }, [dialog, taxonomyAttempt]);

  const handleCreateTag = async (name: string): Promise<LibraryTag> => {
    const result = await createLibraryTagResolvingConflict(name);
    setTaxonomy((current) => {
      if (current.status !== "ready") return current;
      if (result.latestTags !== null) {
        return { ...current, tags: result.latestTags };
      }
      const tags = current.tags.filter((tag) => tag.id !== result.tag.id);
      tags.push(result.tag);
      tags.sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
      return { ...current, tags };
    });
    return result.tag;
  };

  const readySite = state.status === "ready" ? state.site : null;
  const previewUrl = readySite && isSafePreviewUrl(readySite.previewUrl) ? readySite.previewUrl : null;
  const previewAvailable = Boolean(previewUrl && previewUrl !== failedPreviewUrl);
  const lightboxOpen = previewOpen && previewAvailable;

  useEffect(() => {
    if (!lightboxOpen) return;

    const previewTrigger = previewTriggerRef.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    previewCloseRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPreviewOpen(false);
    };
    window.addEventListener("keydown", handleKeyDown);

    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      previewTrigger?.focus();
    };
  }, [lightboxOpen]);

  const handlePreviewError = (url: string) => {
    setFailedPreviewUrl(url);
    setPreviewOpen(false);
  };

  // 快捷键 o / s / e / z 作用于这条收藏（焦点不在别的列表项上时）
  const shortcutRef = useShortcutTarget<HTMLElement>((command) => {
    if (!readySite) return;
    if (command === "open") quickActions.open(readySite);
    else if (command === "star") void quickActions.toggleStar(readySite);
    else if (command === "seen") {
      void (readySite.seenAt ? quickActions.unmarkSeen(readySite) : quickActions.markSeen(readySite));
    } else void quickActions.snooze(readySite);
  });

  const openDialog = (kind: Exclude<DialogKind, null>) => {
    setMutationError(null);
    setNotice(null);
    if (kind === "edit") setTaxonomy({ status: "loading" });
    if (kind === "space") setSpaceDialogSeq((current) => current + 1);
    setDialog(kind);
  };

  const closeDialog = () => {
    if (busy) return;
    setDialog(null);
    setMutationError(null);
  };

  /** 抓取公开网页，再由账号模型原子补全分类、标签、简介和详细介绍。 */
  const handleAnalyze = async (id: string) => {
    if (analyzing) return;
    const generation = analysisGenerationRef.current + 1;
    analysisGenerationRef.current = generation;
    setAnalyzing(true);
    setAnalysisStartedAt(Date.now());
    setAnalysisElapsedSeconds(0);
    setNotice(null);
    try {
      const result = await analyzeLibrarySite(id);
      setState({ status: "ready", site: result.site });
      setNotice({
        tone:
          result.outcome === "complete" && result.llmApplied
            ? "success"
            : result.outcome === "limited" || result.outcome === "complete"
              ? "warning"
              : "error",
        message: result.llmApplied
          ? result.outcome === "complete"
            ? "AI 分析入库已完成；分类、标签、简介和详细介绍中可写的字段已更新，人工维护内容已保留。"
            : `AI 分类、标签、简介和详细介绍已处理；${result.message}`
          : result.outcome === "complete"
            ? "网页资料已更新，但分类、标签、简介和详细介绍没有确认到 LLM 结果入库，请重试或检查 Provider。"
            : result.message,
      });
    } catch (error) {
      setNotice({
        tone: "error",
        message: error instanceof Error ? error.message : "AI 分析入库失败，请稍后重试。",
      });
    } finally {
      if (analysisGenerationRef.current === generation) {
        analysisGenerationRef.current += 1;
        setAnalyzing(false);
        setAnalysisStartedAt(null);
      }
    }
  };

  /** 版本冲突后重新拉最新版本：既刷新页面数据，也让弹层里的表单基于新 version 重试 */
  const recoverConflict = async (id: string, action: "edit" | "delete") => {
    try {
      const latest = await getLibrarySite(id);
      setState({ status: "ready", site: latest });
      setMutationError(
        action === "edit"
          ? "这条收藏刚被其他操作更新，已载入最新版本，请核对后再次保存。"
          : "这条收藏刚被其他操作更新，已载入最新版本，请再次确认删除。",
      );
    } catch (error) {
      if (isLibraryNotFound(error)) {
        setDialog(null);
        setState({ status: "not-found", message: "这条收藏已在其他页面被删除。" });
        return;
      }
      setMutationError(error instanceof Error ? error.message : "读取最新版本失败，请稍后重试。");
    }
  };

  const handleUpdate = async (input: LibrarySiteUpdateInput) => {
    if (state.status !== "ready" || busy) return;
    const id = state.site.id;
    setBusy(true);
    setMutationError(null);
    try {
      const updated = await updateLibrarySite(id, input);
      setState({ status: "ready", site: updated });
      setDialog(null);
      setNotice({ tone: "success", message: "收藏信息已更新" });
    } catch (error) {
      if (isLibraryErrorCode(error, "version_conflict")) {
        await recoverConflict(id, "edit");
      } else if (isLibraryNotFound(error)) {
        setDialog(null);
        setState({ status: "not-found", message: "这条收藏已在其他页面被删除。" });
      } else {
        setMutationError(error instanceof Error ? error.message : "保存失败，请稍后重试。");
      }
    } finally {
      setBusy(false);
    }
  };

  /** 分类 / 标签就地编辑：只发被改的字段；冲突时载入最新版本并提示重试。 */
  const handleInlineUpdate = async (input: LibrarySiteUpdateInput): Promise<boolean> => {
    if (state.status !== "ready") return false;
    const id = state.site.id;
    setNotice(null);
    try {
      const updated = await updateLibrarySite(id, input);
      setState({ status: "ready", site: updated });
      setNotice({
        tone: "success",
        message: input.categoryId !== undefined ? `已移到「${updated.category.name}」` : "标签已更新",
      });
      return true;
    } catch (error) {
      if (isLibraryNotFound(error)) {
        setState({ status: "not-found", message: "这条收藏已在其他页面被删除。" });
        return false;
      }
      if (isLibraryErrorCode(error, "version_conflict")) {
        try {
          setState({ status: "ready", site: await getLibrarySite(id) });
        } catch {
          // 保留当前页面，提示里仍然要求用户重试
        }
        setNotice({ tone: "warning", message: "这条收藏刚被其他操作更新，已载入最新版本，请再改一次。" });
        return false;
      }
      setNotice({
        tone: "error",
        message: error instanceof Error ? error.message : "没有保存成功，请稍后重试。",
      });
      return false;
    }
  };

  const handleDelete = async () => {
    if (state.status !== "ready" || busy) return;
    const { id, version } = state.site;
    setBusy(true);
    setMutationError(null);
    try {
      await deleteLibrarySite(id, version);
      router.replace("/library");
    } catch (error) {
      if (isLibraryErrorCode(error, "version_conflict")) {
        await recoverConflict(id, "delete");
      } else if (isLibraryNotFound(error)) {
        // 目标已经不在了，效果等同删除成功
        router.replace("/library");
      } else {
        setMutationError(error instanceof Error ? error.message : "删除失败，请稍后重试。");
      }
    } finally {
      setBusy(false);
    }
  };

  if (!normalizedSiteId) {
    return (
      <DetailStatus
        state={{ status: "not-found", message: "这个收藏链接无效，请返回收藏库重新选择。" }}
        onRetry={retry}
      />
    );
  }
  if (state.status !== "ready") return <DetailStatus state={state} onRetry={retry} />;

  const { site } = state;
  const host = hostOf(site.identityUrl || site.originalUrl);
  const title = resourceTitle(site);
  const saved = formatRelativeTime(site.createdAt, now);
  const unopened = isUnopened(site);
  const seen = site.seenAt !== null;
  // 封面已经画出名称（仓库）或简介钩子（要点）时，标题行与简介不再重复一遍
  const coverType = coverModel(site, "hero").type;
  const analyzeLabel = analyzing
    ? "AI 分析中"
    : site.analysisStatus === "not_analyzed" || site.analysisStatus === "failed"
      ? "AI 分析入库"
      : "重新 AI 分析";

  return (
    <main className="sd-page" ref={shortcutRef} data-shortcut-default>
      <nav className="sd-breadcrumb" aria-label="面包屑">
        <Link href="/library">收藏库</Link>
        <ChevronRight size={16} aria-hidden="true" />
        <Link href={`/library?category=${encodeURIComponent(site.category.id)}`}>{site.category.name}</Link>
        <ChevronRight size={16} aria-hidden="true" />
        <span className="sd-breadcrumb-current">{title}</span>
      </nav>

      <div className="sd-cover">
        <ResourceCover site={site} size="hero" hideBlurb />
        {previewAvailable && previewUrl && (
          <button
            ref={previewTriggerRef}
            type="button"
            className="sd-cover-zoom"
            aria-label={`放大查看 ${title} 的网页预览图`}
            title="放大查看预览图"
            onClick={() => setPreviewOpen(true)}
          >
            <ZoomIn size={16} aria-hidden="true" />
          </button>
        )}
      </div>

      <header className="sd-heading">
        <div className="sd-heading-copy">
          <h1 id="sd-title" className={coverType === "repo" ? "sr-only" : "sd-title"}>{title}</h1>
          <p className="sd-meta-line">
            <span>{resourceKindLabel(site)}</span>
            <span aria-hidden="true">·</span>
            <span className="sd-meta-host">
              <SiteFavicon url={site.faviconUrl} name={site.name} size={16} />
              {host}
            </span>
            {saved && (
              <>
                <span aria-hidden="true">·</span>
                <time dateTime={site.createdAt} title={formatDay(site.createdAt)}>{saved}收藏</time>
              </>
            )}
            <span aria-hidden="true">·</span>
            <span>{SOURCE_LABELS[site.source]}</span>
          </p>
          {(unopened || site.pinned) && (
            <p className="sd-badges">
              {unopened && (
                <span className="sd-badge sd-badge-accent">
                  <span className="sd-badge-dot" aria-hidden="true" />
                  未打开
                </span>
              )}
              {site.pinned && (
                <span className="sd-badge">
                  <Pin size={12} aria-hidden="true" />
                  已置顶
                </span>
              )}
            </p>
          )}
        </div>

        <div className="sd-actions" role="group" aria-label="收藏操作">
          <a
            className="sd-btn sd-btn-primary"
            href={site.originalUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={() => quickActions.recordOpen(site)}
            onAuxClick={(event) => {
              if (event.button === 1) quickActions.recordOpen(site);
            }}
            title="打开原站（o）"
          >
            打开
            <ArrowUpRight size={16} aria-hidden="true" />
            <span className="sr-only">（在新标签页打开原站）</span>
          </a>
          <button
            type="button"
            className="sd-btn sd-btn-secondary"
            aria-pressed={site.starred}
            data-active={site.starred || undefined}
            onClick={() => void quickActions.toggleStar(site)}
            title={site.starred ? "取消星标（s）" : "加星标（s）"}
          >
            <Star size={16} aria-hidden="true" fill={site.starred ? "currentColor" : "none"} />
            {site.starred ? "已星标" : "星标"}
          </button>
          <button
            type="button"
            className="sd-btn sd-btn-secondary"
            aria-pressed={seen}
            data-active={seen || undefined}
            onClick={() => void (seen ? quickActions.unmarkSeen(site) : quickActions.markSeen(site))}
            title={seen ? "改回未看（e）" : "标记已看（e）"}
          >
            {seen ? <Check size={16} aria-hidden="true" /> : <Eye size={16} aria-hidden="true" />}
            {seen ? "已看过" : "已看"}
          </button>
          <button
            type="button"
            className="sd-btn sd-btn-secondary"
            onClick={() => void quickActions.snooze(site)}
            title="推迟到下周再提醒（z）"
          >
            <AlarmClock size={16} aria-hidden="true" />
            推迟
          </button>
          <button type="button" className="sd-btn sd-btn-secondary" onClick={() => openDialog("edit")}>
            <Pencil size={16} aria-hidden="true" />
            编辑
          </button>
          <MoreMenu
            analyzing={analyzing}
            analyzeLabel={analyzeLabel}
            onAnalyze={() => void handleAnalyze(site.id)}
            onJoinSpace={() => openDialog("space")}
            onDelete={() => openDialog("delete")}
          />
        </div>
      </header>

      {notice && (
        <p
          className="sd-notice"
          data-tone={notice.tone}
          role={notice.tone === "error" ? "alert" : "status"}
        >
          {notice.tone === "success" ? (
            <Check size={16} aria-hidden="true" />
          ) : (
            <CircleAlert size={16} aria-hidden="true" />
          )}
          {notice.message}
        </p>
      )}

      <div className="sd-layout">
        <div className="sd-main">
          <section className="sd-card sd-section" aria-labelledby="sd-desc-title">
            <header className="sd-section-head">
              <h2 id="sd-desc-title" className="sd-section-title">简介与详细介绍</h2>
              <button
                className="sd-ai-analysis-action"
                type="button"
                disabled={analyzing}
                aria-busy={analyzing}
                onClick={() => void handleAnalyze(site.id)}
                title={
                  site.description
                    ? "重新抓取公开网页，并用模型分析分类、标签、简介和详细介绍；人工内容不会被覆盖"
                    : `暂无详细介绍，点击「${analyzeLabel}」让模型根据网页公开内容补全；人工内容不会被覆盖`
                }
                aria-label={site.description ? undefined : `${analyzeLabel}：暂无详细介绍，让模型根据网页公开内容补全`}
              >
                {analyzing ? <Spinner size={13} /> : <WandSparkles size={13} aria-hidden="true" />}
                {analyzeLabel}
              </button>
            </header>
            {analyzing && (
              <SiteAnalysisProgress
                phase={site.analysisPhase ?? "queued"}
                elapsedSeconds={analysisElapsedSeconds}
              />
            )}
            {site.summary ? (
              coverType === "article" ? null : <p className="sd-summary">{site.summary}</p>
            ) : (
              <p className="sd-desc sd-desc-empty">还没有简介。</p>
            )}
            {site.description && <p className="sd-desc">{site.description}</p>}
          </section>

          <TaxonomyEditor site={site} onUpdate={handleInlineUpdate} />
          <RepoPanel site={site} now={now} />
          <UsageCard site={site} now={now} />
        </div>

        <aside className="sd-side" aria-label="相关信息">
          {/* key：改分类后重挂载，回到 loading 态重新拉同分类收藏 */}
          <RelatedSitesCard
            key={site.category.id}
            siteId={site.id}
            categoryId={site.category.id}
            categoryName={site.category.name}
          />
          <AgentHelpCard siteName={site.name} />
        </aside>
      </div>

      <LibraryDialog
        open={dialog === "edit"}
        title="编辑收藏"
        description="修改会立即保存到当前账号的收藏库。"
        size="wide"
        closeDisabled={busy}
        onClose={closeDialog}
      >
        {dialog === "edit" && taxonomy.status === "loading" && (
          <p className="sd-hint" role="status">
            <Spinner size={16} />
            正在加载分类与标签…
          </p>
        )}
        {dialog === "edit" && taxonomy.status === "error" && (
          <div className="sd-inline-error" role="alert">
            <p>{taxonomy.message}</p>
            <button
              type="button"
              className="sd-btn sd-btn-secondary sd-btn-small"
              onClick={() => {
                setTaxonomy({ status: "loading" });
                setTaxonomyAttempt((current) => current + 1);
              }}
            >
              <RefreshCw size={16} aria-hidden="true" />
              重试
            </button>
          </div>
        )}
        {dialog === "edit" && taxonomy.status === "ready" && (
          <SiteForm
            site={site}
            categories={taxonomy.categories}
            tags={taxonomy.tags}
            busy={busy}
            error={mutationError}
            onCancel={closeDialog}
            onCreateTag={handleCreateTag}
            onUpdate={handleUpdate}
          />
        )}
      </LibraryDialog>

      <LibraryDialog
        open={dialog === "delete"}
        title="删除收藏"
        description="此操作会从当前账号的收藏库中移除这条收藏。"
        closeDisabled={busy}
        onClose={closeDialog}
      >
        {dialog === "delete" && (
          <div>
            <div className="sd-delete-site">
              <SiteFavicon url={site.faviconUrl} name={site.name} size={28} />
              <div className="sd-delete-info">
                <strong>{title}</strong>
                <span>{host}</span>
              </div>
            </div>
            <p className="sd-dialog-text">删除后无法恢复：打开记录、星标和它在 Space 里的位置都会一起移除。</p>
            {mutationError && (
              <p className="sd-form-error" role="alert">
                {mutationError}
              </p>
            )}
            <footer className="sd-dialog-actions">
              <button type="button" className="sd-btn sd-btn-secondary" onClick={closeDialog} disabled={busy}>
                取消
              </button>
              <button
                type="button"
                className="sd-btn sd-btn-danger"
                onClick={() => void handleDelete()}
                disabled={busy}
              >
                {busy ? <Spinner size={16} /> : <Trash2 size={16} aria-hidden="true" />}
                {busy ? "正在删除" : "确认删除"}
              </button>
            </footer>
          </div>
        )}
      </LibraryDialog>

      <JoinSpaceDialog
        key={spaceDialogSeq}
        open={dialog === "space"}
        siteId={site.id}
        siteName={site.name}
        onClose={closeDialog}
        onJoined={(spaceName) => {
          setDialog(null);
          setNotice({ tone: "success", message: `已加入 Space「${spaceName}」` });
        }}
      />

      {lightboxOpen && previewUrl && (
        <div
          className="sd-preview-overlay"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setPreviewOpen(false);
          }}
        >
          <div
            className="sd-preview-dialog"
            role="dialog"
            aria-modal="true"
            aria-label={`${title} 网页预览图`}
            tabIndex={-1}
            onKeyDown={(event) => {
              if (event.key === "Tab") {
                event.preventDefault();
                previewCloseRef.current?.focus();
              }
            }}
          >
            <button
              ref={previewCloseRef}
              type="button"
              className="sd-preview-close"
              aria-label="关闭预览图"
              title="关闭"
              onClick={() => setPreviewOpen(false)}
            >
              <X size={20} aria-hidden="true" />
            </button>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={previewUrl}
              alt={`${title} 网页预览大图`}
              width={1200}
              height={675}
              referrerPolicy="no-referrer"
              onError={() => handlePreviewError(previewUrl)}
            />
          </div>
        </div>
      )}
    </main>
  );
}
