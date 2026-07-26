"use client";

import {
  ArrowLeft,
  CalendarDays,
  CircleAlert,
  ExternalLink,
  Folder,
  LoaderCircle,
  Pin,
  RefreshCw,
  Tags,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { SiteFavicon } from "@/components/library/site-favicon";
import type { LibraryAnalysisStatus, LibrarySite, LibrarySiteSource } from "@/lib/library-contract";
import { getLibrarySite, LibraryApiError } from "@/lib/library-client";

type SiteDetailProps = {
  siteId: string;
};

type LoadState =
  | { status: "loading" }
  | { status: "ready"; site: LibrarySite }
  | { status: "not-found"; message: string }
  | { status: "error"; message: string };

type StatusState = Exclude<LoadState, { status: "ready" }>;

const SOURCE_LABELS: Record<LibrarySiteSource, string> = {
  manual: "手动添加",
  agent: "Agent 存入",
  browser_import: "浏览器导入",
  backup: "备份恢复",
};

const ANALYSIS_STATUS_LABELS: Record<LibraryAnalysisStatus, string> = {
  not_analyzed: "未分析",
  pending: "分析中",
  complete: "分析完成",
  failed: "分析失败",
  limited: "部分完成",
};

const DATE_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "long",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

function validSiteId(value: string): string | null {
  const normalized = value.trim();
  if (!normalized || normalized.length > 256 || /[\u0000-\u001f\u007f]/u.test(normalized)) return null;
  return normalized;
}

function formatDate(value: string): string {
  return DATE_FORMATTER.format(new Date(value));
}

function DetailStatus({ state, onRetry }: Readonly<{ state: StatusState; onRetry: () => void }>) {
  const loading = state.status === "loading";

  return (
    <main className="site-main">
      <Link
        href="/library"
        className="inline-flex min-h-9 items-center gap-2 rounded-[var(--radius-sm)] px-2 text-sm font-semibold text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
      >
        <ArrowLeft className="size-4" aria-hidden="true" />
        返回资料库
      </Link>
      <section
        className="mx-auto flex min-h-[52vh] w-full max-w-xl flex-col items-center justify-center py-16 text-center"
        aria-live="polite"
        aria-busy={loading}
      >
        <span
          className="mb-4 grid size-10 place-items-center rounded-[var(--radius-md)] bg-[var(--surface-subtle)] text-[var(--text-secondary)]"
          aria-hidden="true"
        >
          {loading ? <LoaderCircle className="size-5 animate-spin" /> : <CircleAlert className="size-5" />}
        </span>
        <h1 className="m-0 text-xl font-bold text-[var(--text)]">
          {loading ? "正在读取站点" : state.status === "not-found" ? "站点不存在" : "暂时无法读取站点"}
        </h1>
        <p className="mt-2 max-w-md text-sm leading-6 text-[var(--text-muted)]">
          {loading ? "正在同步当前账号中的资料库数据。" : state.message}
        </p>
        {state.status === "error" && (
          <button
            type="button"
            className="mt-5 inline-flex min-h-9 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] bg-[var(--action)] px-3 font-semibold text-[var(--on-action)] hover:bg-[var(--action-hover)]"
            onClick={onRetry}
          >
            <RefreshCw className="size-4" aria-hidden="true" />
            重新加载
          </button>
        )}
      </section>
    </main>
  );
}

function MetadataRow({
  term,
  children,
}: Readonly<{ term: string; children: ReactNode }>) {
  return (
    <div className="grid min-w-0 gap-1 border-b border-[var(--line)] py-4 last:border-b-0 sm:grid-cols-[8rem_minmax(0,1fr)] sm:gap-5">
      <dt className="text-xs font-semibold text-[var(--text-muted)]">{term}</dt>
      <dd className="m-0 min-w-0 text-sm leading-6 text-[var(--text-secondary)]">{children}</dd>
    </div>
  );
}

function SiteDetailContent({ site }: Readonly<{ site: LibrarySite }>) {
  return (
    <main className="site-main min-w-0">
      <Link
        href="/library"
        className="inline-flex min-h-9 items-center gap-2 rounded-[var(--radius-sm)] px-2 text-sm font-semibold text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
      >
        <ArrowLeft className="size-4" aria-hidden="true" />
        返回资料库
      </Link>

      <article className="mt-5 min-w-0" aria-labelledby="site-detail-title">
        <header className="grid min-w-0 gap-6 border-b border-[var(--line)] pb-8 md:grid-cols-[minmax(0,1fr)_auto] md:items-start">
          <div className="flex min-w-0 items-start gap-4">
            <span className="grid size-14 shrink-0 place-items-center overflow-hidden rounded-[var(--radius-md)] border border-[var(--line)] bg-[var(--surface-raised)] [&_.site-favicon]:grid [&_.site-favicon]:size-10 [&_.site-favicon]:place-items-center [&_.site-favicon]:overflow-hidden [&_.site-favicon]:rounded-[var(--radius-sm)] [&_.site-favicon]:bg-[var(--surface-subtle)] [&_.site-favicon]:text-lg [&_.site-favicon]:font-bold [&_.site-favicon_img]:size-full [&_.site-favicon_img]:object-contain [&_.site-favicon_svg]:size-5">
              <SiteFavicon url={site.faviconUrl} name={site.name} size="large" />
            </span>
            <div className="min-w-0 pt-0.5">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <h1
                  id="site-detail-title"
                  className="m-0 min-w-0 [overflow-wrap:anywhere] text-2xl font-bold leading-tight text-[var(--text)] sm:text-[1.75rem]"
                >
                  {site.name}
                </h1>
                {site.pinned && (
                  <span className="inline-flex shrink-0 items-center gap-1 rounded-[var(--radius-sm)] bg-[var(--accent-soft)] px-2 py-1 text-xs font-bold text-[var(--accent-strong)]">
                    <Pin className="size-3" aria-hidden="true" />
                    已置顶
                  </span>
                )}
              </div>
              <a
                href={site.originalUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-2 inline-flex max-w-full items-start gap-1.5 text-sm text-[var(--text-muted)] underline decoration-[var(--line-strong)] underline-offset-4 hover:text-[var(--text)]"
              >
                <span className="min-w-0 [overflow-wrap:anywhere]">{site.originalUrl}</span>
                <ExternalLink className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
                <span className="sr-only">（在新标签页打开）</span>
              </a>
            </div>
          </div>

          <a
            href={site.originalUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="site-detail-primary-action inline-flex min-h-10 w-full items-center justify-center gap-2 rounded-[var(--radius-sm)] bg-[var(--action)] px-4 font-semibold hover:bg-[var(--action-hover)] md:w-auto"
          >
            访问网站
            <ExternalLink className="size-4" aria-hidden="true" />
            <span className="sr-only">（在新标签页打开）</span>
          </a>
        </header>

        <div className="grid min-w-0 gap-10 py-8 lg:grid-cols-[minmax(0,1fr)_20rem] lg:gap-14">
          <section className="min-w-0" aria-labelledby="site-description-title">
            <h2 id="site-description-title" className="m-0 text-base font-bold text-[var(--text)]">
              站点简介
            </h2>
            <p className="mt-3 whitespace-pre-wrap [overflow-wrap:anywhere] text-[0.9375rem] leading-7 text-[var(--text-secondary)]">
              {site.description ?? "暂未添加简介。"}
            </p>
          </section>

          <aside className="min-w-0 border-t border-[var(--line)] pt-1 lg:border-t-0 lg:border-l lg:pl-8" aria-label="站点信息">
            <dl className="m-0 min-w-0">
              <MetadataRow term="分类">
                <span className="inline-flex items-center gap-2 [overflow-wrap:anywhere]">
                  <Folder className="size-4 shrink-0 text-[var(--text-muted)]" aria-hidden="true" />
                  {site.category.name}
                  {site.category.isDefault && <span className="text-xs text-[var(--text-muted)]">默认</span>}
                </span>
              </MetadataRow>
              <MetadataRow term="标签">
                {site.tags.length > 0 ? (
                  <span className="flex min-w-0 flex-wrap gap-1.5">
                    {site.tags.map((tag) => (
                      <span
                        key={tag.id}
                        className="max-w-full [overflow-wrap:anywhere] rounded-[var(--radius-sm)] bg-[var(--surface-subtle)] px-2 py-0.5 text-xs text-[var(--text-secondary)]"
                      >
                        {tag.name}
                      </span>
                    ))}
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-2 text-[var(--text-muted)]">
                    <Tags className="size-4" aria-hidden="true" />
                    暂无标签
                  </span>
                )}
              </MetadataRow>
              <MetadataRow term="置顶状态">{site.pinned ? "已置顶" : "未置顶"}</MetadataRow>
              <MetadataRow term="添加方式">{SOURCE_LABELS[site.source]}</MetadataRow>
              <MetadataRow term="内容分析">{ANALYSIS_STATUS_LABELS[site.analysisStatus]}</MetadataRow>
              <MetadataRow term="创建时间">
                <span className="inline-flex items-start gap-2">
                  <CalendarDays className="mt-1 size-4 shrink-0 text-[var(--text-muted)]" aria-hidden="true" />
                  <time dateTime={site.createdAt}>{formatDate(site.createdAt)}</time>
                </span>
              </MetadataRow>
              <MetadataRow term="更新时间">
                <time dateTime={site.updatedAt}>{formatDate(site.updatedAt)}</time>
              </MetadataRow>
            </dl>
          </aside>
        </div>
      </article>
    </main>
  );
}

export function SiteDetail({ siteId }: Readonly<SiteDetailProps>) {
  const normalizedSiteId = validSiteId(siteId);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [state, setState] = useState<LoadState>({ status: "loading" });

  const retry = useCallback(() => {
    setState({ status: "loading" });
    setLoadAttempt((attempt) => attempt + 1);
  }, []);

  useEffect(() => {
    if (!normalizedSiteId) return;
    const controller = new AbortController();

    void getLibrarySite(normalizedSiteId, controller.signal)
      .then((site) => setState({ status: "ready", site }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof LibraryApiError && error.status === 404) {
          setState({ status: "not-found", message: "这个站点可能已被删除，或不属于当前登录账号。" });
          return;
        }
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "资料库服务暂时不可用，请稍后重试。",
        });
      });

    return () => controller.abort();
  }, [loadAttempt, normalizedSiteId]);

  if (!normalizedSiteId) {
    return (
      <DetailStatus
        state={{ status: "not-found", message: "这个资料库链接无效，请返回资料库重新选择站点。" }}
        onRetry={retry}
      />
    );
  }
  if (state.status !== "ready") return <DetailStatus state={state} onRetry={retry} />;
  return <SiteDetailContent site={state.site} />;
}
