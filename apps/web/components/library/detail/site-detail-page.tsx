"use client";

import {
  ArrowLeft,
  ArrowUpRight,
  Box,
  Check,
  ChevronRight,
  CircleAlert,
  Pencil,
  Pin,
  RefreshCw,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { LibraryDialog } from "@/components/library/library-dialog";
import { SiteForm } from "@/components/library/site-form";
import { Spinner } from "@/components/react-bits/spinner";
import { SiteFavicon } from "@/components/site-favicon";
import {
  analyzeLibrarySite,
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

import { ActivityCard } from "./activity-card";
import { AgentHelpCard } from "./agent-help-card";
import { JoinSpaceDialog } from "./join-space-dialog";
import { ANALYSIS_STATUS_LABELS, formatDay, hostOf, SOURCE_LABELS } from "./meta";
import { RelatedSitesCard } from "./related-sites-card";

/* 网站详情页（设计稿 1d）。
   数据链路与旧版保持一致：getLibrarySite 取数、非法/404/错误三种兜底、
   编辑与删除都带 expectedVersion 做乐观并发；外观按设计稿主列三卡装配：
   主信息卡 → 详细介绍 → 收录动态（设计稿没有「网站预览」卡，不自创）。 */

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

function DetailStatus({ state, onRetry }: Readonly<{ state: StatusState; onRetry: () => void }>) {
  const loading = state.status === "loading";

  return (
    <main className="sd-page">
      <Link href="/library" className="sd-back-link">
        <ArrowLeft size={16} aria-hidden="true" />
        返回网址库
      </Link>
      <section className="sd-status" aria-live="polite" aria-busy={loading}>
        <span className="sd-status-icon" aria-hidden="true">
          {loading ? <Spinner size={16} /> : <CircleAlert size={16} />}
        </span>
        <h1 className="sd-status-title">
          {loading ? "正在读取网站" : state.status === "not-found" ? "网站不存在" : "暂时无法读取网站"}
        </h1>
        <p className="sd-status-text">
          {loading ? "正在同步当前账号网址库中的收录信息。" : state.message}
        </p>
        {state.status === "error" && (
          <button type="button" className="sd-btn sd-btn-primary sd-status-retry" onClick={onRetry}>
            <RefreshCw size={16} aria-hidden="true" />
            重新加载
          </button>
        )}
      </section>
    </main>
  );
}

export function SiteDetailPage({ siteId }: Readonly<SiteDetailPageProps>) {
  const router = useRouter();
  const normalizedSiteId = validSiteId(siteId);

  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [loadAttempt, setLoadAttempt] = useState(0);

  const [dialog, setDialog] = useState<DialogKind>(null);
  const [busy, setBusy] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);

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
      .then((site) => setState({ status: "ready", site }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (isLibraryNotFound(error)) {
          setState({ status: "not-found", message: "这个网站可能已被删除，或不属于当前登录账号。" });
          return;
        }
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "网址库服务暂时不可用，请稍后重试。",
        });
      });

    return () => controller.abort();
  }, [loadAttempt, normalizedSiteId]);

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

  /** 重新读取网页公开元数据。只填空，不覆盖用户自己写过的说明（服务端保证）。 */
  const handleAnalyze = async (id: string) => {
    if (analyzing) return;
    setAnalyzing(true);
    setNotice(null);
    try {
      const latest = await analyzeLibrarySite(id);
      setState({ status: "ready", site: latest });
      setNotice(
        latest.analysisStatus === "complete"
          ? "已读取网页信息"
          : latest.analysisStatus === "limited"
            ? "只读到部分信息：这个页面可能需要执行脚本才能渲染内容"
            : "没能读取到网页信息，稍后可以再试",
      );
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "分析失败，请稍后重试。");
    } finally {
      setAnalyzing(false);
    }
  };

  /** 版本冲突后重新拉最新版本：既刷新页面数据，也让弹层里的表单基于新 version 重试 */
  const recoverConflict = async (id: string, action: "edit" | "delete") => {
    try {
      const latest = await getLibrarySite(id);
      setState({ status: "ready", site: latest });
      setMutationError(
        action === "edit"
          ? "该网站刚被其他操作更新，已载入最新版本，请核对后再次保存。"
          : "该网站刚被其他操作更新，已载入最新版本，请再次确认删除。",
      );
    } catch (error) {
      if (isLibraryNotFound(error)) {
        setDialog(null);
        setState({ status: "not-found", message: "该网站已在其他页面被删除。" });
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
      setNotice("网站信息已更新");
    } catch (error) {
      if (isLibraryErrorCode(error, "version_conflict")) {
        await recoverConflict(id, "edit");
      } else if (isLibraryNotFound(error)) {
        setDialog(null);
        setState({ status: "not-found", message: "该网站已在其他页面被删除。" });
      } else {
        setMutationError(error instanceof Error ? error.message : "保存失败，请稍后重试。");
      }
    } finally {
      setBusy(false);
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
        state={{ status: "not-found", message: "这个网址库链接无效，请返回网址库重新选择网站。" }}
        onRetry={retry}
      />
    );
  }
  if (state.status !== "ready") return <DetailStatus state={state} onRetry={retry} />;

  const { site } = state;
  const host = hostOf(site.identityUrl || site.originalUrl);

  return (
    <main className="sd-page">
      {/* 面包屑：网址库当前没有按分类筛选的 URL 参数，分类一级先同样指向网址库 */}
      <nav className="sd-breadcrumb" aria-label="面包屑">
        <Link href="/library">网址库</Link>
        <ChevronRight size={16} aria-hidden="true" />
        <Link href="/library">{site.category.name}</Link>
        <ChevronRight size={16} aria-hidden="true" />
        <span className="sd-breadcrumb-current">{site.name}</span>
      </nav>

      {notice && (
        <p className="sd-notice" role="status">
          <Check size={16} aria-hidden="true" />
          {notice}
        </p>
      )}

      <div className="sd-layout">
        <div className="sd-main">
          <section className="sd-card sd-hero" aria-labelledby="sd-title">
            <div className="sd-hero-top">
              <span className="sd-hero-icon">
                <SiteFavicon url={site.faviconUrl} name={site.name} size={40} />
              </span>
              <div className="sd-hero-heading">
                <div className="sd-hero-title-row">
                  <h1 id="sd-title" className="sd-hero-title">
                    {site.name}
                  </h1>
                  {site.pinned && (
                    <span className="sd-pin-badge">
                      <Pin size={16} aria-hidden="true" />
                      已置顶
                    </span>
                  )}
                </div>
                <p className="sd-hero-domain">
                  <a href={site.originalUrl} target="_blank" rel="noopener noreferrer">
                    {host}
                    <span className="sr-only">（在新标签页打开）</span>
                  </a>
                </p>
                {/* 后端只有一个 description 字段：这里截断做导语，全文在下方「详细介绍」卡展示 */}
                {site.description && <p className="sd-hero-desc">{site.description}</p>}
              </div>
              <div className="sd-hero-actions">
                <a
                  className="sd-btn sd-btn-primary"
                  href={site.originalUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  访问官网
                  <ArrowUpRight size={16} aria-hidden="true" />
                  <span className="sr-only">（在新标签页打开）</span>
                </a>
                <button type="button" className="sd-btn sd-btn-secondary" onClick={() => openDialog("edit")}>
                  <Pencil size={16} aria-hidden="true" />
                  编辑
                </button>
                <button type="button" className="sd-btn sd-btn-secondary" onClick={() => openDialog("space")}>
                  <Box size={16} aria-hidden="true" />
                  加入 Space
                </button>
                <button
                  type="button"
                  className="sd-btn sd-btn-danger-ghost"
                  onClick={() => openDialog("delete")}
                  aria-label="删除网站"
                  title="删除网站"
                >
                  <Trash2 size={16} aria-hidden="true" />
                </button>
              </div>
            </div>

            <div className="sd-hero-meta">
              <div>
                <div className="sd-meta-label">分类</div>
                <span className="sd-chip sd-chip-solid">
                  {site.category.name}
                  {site.category.isDefault && <span className="sd-meta-default">默认</span>}
                </span>
              </div>
              <div>
                <div className="sd-meta-label">标签</div>
                {site.tags.length > 0 ? (
                  <div className="sd-chip-row">
                    {site.tags.map((tag) => (
                      <span key={tag.id} className="sd-chip sd-chip-outline">
                        {tag.name}
                      </span>
                    ))}
                  </div>
                ) : (
                  <span className="sd-meta-empty">暂无标签</span>
                )}
              </div>
              {/* 设计稿中的「所属 Space」列：后端没有站点→Space 的反查接口，
                  逐个拉全部 Space 的成员来判断代价过高，先不展示，避免造假 */}
              <div className="sd-meta-fill" aria-hidden="true" />
              <div className="sd-meta-times">
                <div>
                  添加于 <time dateTime={site.createdAt}>{formatDay(site.createdAt)}</time> · 更新于{" "}
                  <time dateTime={site.updatedAt}>{formatDay(site.updatedAt)}</time>
                </div>
                <div>
                  来源：{SOURCE_LABELS[site.source]} · 内容分析：{ANALYSIS_STATUS_LABELS[site.analysisStatus]}
                  {" "}
                  <button
                    className="sd-inline-action"
                    type="button"
                    disabled={analyzing}
                    onClick={() => void handleAnalyze(site.id)}
                    title="重新读取该网页的标题、说明与图标；不会覆盖你自己填过的内容"
                  >
                    {analyzing ? "分析中…" : "重新分析"}
                  </button>
                </div>
              </div>
            </div>
          </section>

          <section className="sd-card sd-section" aria-labelledby="sd-desc-title">
            <h2 id="sd-desc-title" className="sd-section-title">
              详细介绍
            </h2>
            {site.description ? (
              <p className="sd-desc">{site.description}</p>
            ) : (
              <p className="sd-desc sd-desc-empty">
                暂未填写介绍，可以点击右上角「编辑」补充，或让 Agent 帮你生成。
              </p>
            )}
          </section>

          <ActivityCard site={site} />
        </div>

        <aside className="sd-side" aria-label="相关信息">
          {/* key：编辑改分类后重挂载，回到 loading 态重新拉同分类网站 */}
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
        title="编辑网站"
        description="修改会立即保存到当前账号的网址库。"
        size="wide"
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
            onUpdate={handleUpdate}
          />
        )}
      </LibraryDialog>

      <LibraryDialog
        open={dialog === "delete"}
        title="删除网站"
        description="此操作会从当前账号的网址库中移除该网站。"
        onClose={closeDialog}
      >
        {dialog === "delete" && (
          <div>
            <div className="sd-delete-site">
              <SiteFavicon url={site.faviconUrl} name={site.name} size={28} />
              <div className="sd-delete-info">
                <strong>{site.name}</strong>
                <span>{host}</span>
              </div>
            </div>
            <p className="sd-dialog-text">删除后将无法恢复，该网站会从网址库和相关视图中消失。</p>
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
          setNotice(`已加入 Space「${spaceName}」`);
        }}
      />
    </main>
  );
}
