"use client";

import {
  AlertCircle,
  Check,
  ChevronLeft,
  ChevronRight,
  Layers3,
  RefreshCw,
  ScanSearch,
  ShieldCheck,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Spinner } from "@/components/react-bits/spinner";
import { SiteFavicon } from "@/components/site-favicon";
import {
  applySiteSimilarityScan,
  getActiveSiteSimilarityScan,
  LibraryApiError,
  listSiteSimilarityGroups,
  saveSiteSimilarityDecision,
  selectRecommendedSiteSimilarityDecisions,
  startSiteSimilarityScan,
} from "@/lib/library-client";
import {
  librarySiteCardSummary,
  type SiteSimilarityApplyResult,
  type SiteSimilarityGroup,
  type SiteSimilarityGroupKind,
  type SiteSimilarityScan,
} from "@/lib/library-contract";
import {
  normalizeSiteSimilarityKeepSelection,
  siteSimilarityKeepAction,
  toggleSiteSimilarityKeepSelection,
} from "@/lib/site-similarity-selection";

type GroupFilter = SiteSimilarityGroupKind | "all";
type PageToken = number | "ellipsis-start" | "ellipsis-end";

const SIMILARITY_PAGE_SIZE = 12;

type SiteSimilarityReviewProps = {
  onApplied: (result: SiteSimilarityApplyResult) => void | Promise<void>;
  onBusyChange?: (busy: boolean) => void;
};

function displayUrl(value: string): string {
  try {
    const url = new URL(value);
    return `${url.host}${url.pathname}${url.search}${url.hash}`;
  } catch {
    return value;
  }
}

function groupLabel(kind: SiteSimilarityGroupKind): string {
  return kind === "duplicate" ? "高度相似" : "同站页面";
}

function reviewError(error: unknown): string {
  if (
    error instanceof LibraryApiError
    && error.code === "site_similarity_version_conflict"
  ) {
    return "排查结果已在其他窗口更新，请刷新结果后再继续";
  }
  return error instanceof Error ? error.message : "相似网站排查暂时不可用，请稍后重试";
}

function paginationTokens(pageCount: number, currentPage: number): PageToken[] {
  if (pageCount <= 0) return [];
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, index) => index + 1);
  const pages = new Set([1, pageCount, currentPage - 1, currentPage, currentPage + 1]);
  if (currentPage <= 3) [2, 3, 4].forEach((page) => pages.add(page));
  if (currentPage >= pageCount - 2) {
    [pageCount - 3, pageCount - 2, pageCount - 1].forEach((page) => pages.add(page));
  }
  const ordered = [...pages]
    .filter((page) => page >= 1 && page <= pageCount)
    .sort((left, right) => left - right);
  const tokens: PageToken[] = [];
  for (const page of ordered) {
    const previous = tokens[tokens.length - 1];
    if (typeof previous === "number" && page - previous > 1) {
      tokens.push(previous === 1 ? "ellipsis-start" : "ellipsis-end");
    }
    tokens.push(page);
  }
  return tokens;
}

export function SiteSimilarityReview({
  onApplied,
  onBusyChange,
}: SiteSimilarityReviewProps) {
  const [scan, setScan] = useState<SiteSimilarityScan | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [scanBusy, setScanBusy] = useState(false);
  const [groupsLoading, setGroupsLoading] = useState(false);
  const [decisionBusyGroup, setDecisionBusyGroup] = useState<string | null>(null);
  const [bulkDecisionBusy, setBulkDecisionBusy] = useState(false);
  const [applyBusy, setApplyBusy] = useState(false);
  const [filter, setFilter] = useState<GroupFilter>("all");
  const [groups, setGroups] = useState<SiteSimilarityGroup[]>([]);
  const [page, setPage] = useState(1);
  const [pageCount, setPageCount] = useState(0);
  const [groupsRefreshKey, setGroupsRefreshKey] = useState(0);
  const [decisionVersion, setDecisionVersion] = useState(1);
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<SiteSimilarityApplyResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const busy = initialLoading || scanBusy || groupsLoading || decisionBusyGroup !== null
    || bulkDecisionBusy || applyBusy;
  const scanRunId = scan?.status === "ready" ? scan.runId : null;

  useEffect(() => {
    onBusyChange?.(busy);
  }, [busy, onBusyChange]);

  useEffect(() => {
    const controller = new AbortController();
    void getActiveSiteSimilarityScan(controller.signal)
      .then((active) => {
        if (active) setGroupsLoading(true);
        setScan(active);
        if (active) setDecisionVersion(active.decisionVersion);
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setError(reviewError(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) setInitialLoading(false);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!scanRunId || result) return;
    const controller = new AbortController();
    void listSiteSimilarityGroups(
      scanRunId,
      { kind: filter, page, limit: SIMILARITY_PAGE_SIZE },
      controller.signal,
    )
      .then((groupPage) => {
        setGroups(groupPage.items);
        setPageCount(groupPage.totalPages);
        setDecisionVersion(groupPage.decisionVersion);
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setError(reviewError(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) setGroupsLoading(false);
      });
    return () => controller.abort();
  }, [filter, groupsRefreshKey, page, result, scanRunId]);

  const resetPaging = () => {
    setPage(1);
    setPageCount(0);
    setGroups([]);
  };

  const beginScan = async () => {
    setScanBusy(true);
    setError(null);
    setConfirming(false);
    setResult(null);
    setScan(null);
    setGroups([]);
    setGroupsLoading(false);
    try {
      const next = await startSiteSimilarityScan();
      setGroupsLoading(true);
      setScan(next);
      setDecisionVersion(next.decisionVersion);
      setFilter("all");
      resetPaging();
      setGroupsRefreshKey((current) => current + 1);
    } catch (caught) {
      setError(reviewError(caught));
    } finally {
      setScanBusy(false);
    }
  };

  const reloadActive = async () => {
    setScanBusy(true);
    setError(null);
    try {
      const active = await getActiveSiteSimilarityScan();
      setGroupsLoading(Boolean(active));
      setScan(active);
      setDecisionVersion(active?.decisionVersion ?? 1);
      setConfirming(false);
      setResult(null);
      resetPaging();
      if (active) setGroupsRefreshKey((current) => current + 1);
    } catch (caught) {
      setError(reviewError(caught));
    } finally {
      setScanBusy(false);
    }
  };

  const saveKeepSelection = async (
    group: SiteSimilarityGroup,
    requestedKeepSiteIds: string[],
  ) => {
    if (!scan || busy) return;
    const keepSiteIds = normalizeSiteSimilarityKeepSelection(group, requestedKeepSiteIds);
    setDecisionBusyGroup(group.id);
    setError(null);
    setConfirming(false);
    try {
      const decision = await saveSiteSimilarityDecision(scan.runId, group.id, {
        keepSiteIds,
        expectedVersion: decisionVersion,
      });
      setDecisionVersion(decision.decisionVersion);
      setScan((current) => current && current.runId === scan.runId
        ? {
          ...current,
          version: decision.decisionVersion,
          decisionVersion: decision.decisionVersion,
          selectedGroupCount: decision.selectedGroupCount,
          selectedDeleteCount: decision.selectedDeleteCount,
        }
        : current);
      setGroups((current) => current.map((item) => item.id === group.id
        ? { ...item, keepSiteIds: decision.keepSiteIds }
        : item));
    } catch (caught) {
      setError(reviewError(caught));
    } finally {
      setDecisionBusyGroup(null);
    }
  };

  const chooseRecommendedForFilter = async () => {
    if (!scan || busy) return;
    const requestedFilter = filter;
    setBulkDecisionBusy(true);
    setError(null);
    setConfirming(false);
    try {
      const decision = await selectRecommendedSiteSimilarityDecisions(scan.runId, {
        kind: requestedFilter,
        expectedVersion: decisionVersion,
      });
      setDecisionVersion(decision.decisionVersion);
      setScan((current) => current && current.runId === scan.runId
        ? {
          ...current,
          version: decision.decisionVersion,
          decisionVersion: decision.decisionVersion,
          selectedGroupCount: decision.selectedGroupCount,
          selectedDeleteCount: decision.selectedDeleteCount,
        }
        : current);
      setGroups((current) => current.map((group) => (
        requestedFilter === "all" || group.kind === requestedFilter
          ? { ...group, keepSiteIds: [group.recommendedSiteId] }
          : group
      )));
    } catch (caught) {
      setError(reviewError(caught));
    } finally {
      setBulkDecisionBusy(false);
    }
  };

  const goToPage = (nextPage: number) => {
    if (busy || nextPage === page || nextPage < 1 || nextPage > pageCount) return;
    setGroupsLoading(true);
    setError(null);
    setGroups([]);
    setPage(nextPage);
  };

  const applySelection = async () => {
    if (!scan || scan.selectedDeleteCount < 1 || applyBusy) return;
    setApplyBusy(true);
    setError(null);
    try {
      const applied = await applySiteSimilarityScan(scan.runId, decisionVersion);
      setResult(applied);
      setConfirming(false);
      try {
        await onApplied(applied);
      } catch {
        // The merge is already durable. A failed list refresh must not make it
        // look as if the destructive transaction failed.
      }
    } catch (caught) {
      setError(reviewError(caught));
    } finally {
      setApplyBusy(false);
    }
  };

  const filters = useMemo<Array<{ value: GroupFilter; label: string; count: number }>>(() => [
    { value: "all", label: "全部", count: scan?.groupCount ?? 0 },
    { value: "duplicate", label: "高度相似", count: scan?.duplicateGroupCount ?? 0 },
    { value: "same_site", label: "同站页面", count: scan?.sameSiteGroupCount ?? 0 },
  ], [scan]);
  const activeFilter = filters.find((item) => item.value === filter) ?? filters[0];
  const pageTokens = useMemo(() => paginationTokens(pageCount, page), [page, pageCount]);

  if (initialLoading) {
    return (
      <div className="similarity-loading" role="status">
        <Spinner />
        <span>正在读取上次排查进度</span>
      </div>
    );
  }

  if (result) {
    return (
      <section className="similarity-complete" aria-live="polite">
        <span className="similarity-complete-icon"><ShieldCheck aria-hidden="true" /></span>
        <h3>清理已完成</h3>
        <p>保留 {result.keptSiteCount} 个主网站，清理 {result.deletedSiteCount} 个重复或相似页面。</p>
        <button className="library-button secondary" type="button" onClick={() => void beginScan()} disabled={scanBusy}>
          {scanBusy ? <Spinner /> : <ScanSearch aria-hidden="true" />}
          再次排查
        </button>
      </section>
    );
  }

  if (!scan) {
    return (
      <section className="similarity-start">
        <span className="similarity-start-icon"><ScanSearch aria-hidden="true" /></span>
        <h3>扫描当前网址库</h3>
        <p>所有网站默认保留。扫描只使用本地 URL 规则，不会调用模型或访问外部网站。</p>
        {error && <p className="library-form-error" role="alert">{error}</p>}
        <button className="library-button primary" type="button" onClick={() => void beginScan()} disabled={scanBusy}>
          {scanBusy ? <Spinner /> : <ScanSearch aria-hidden="true" />}
          {scanBusy ? "正在排查" : "开始全面排查"}
        </button>
      </section>
    );
  }

  return (
    <div className="similarity-review">
      <section className="similarity-summary" aria-label="排查摘要">
        <div><strong>{scan.sourceSiteCount}</strong><span>已扫描网站</span></div>
        <div><strong>{scan.duplicateGroupCount}</strong><span>高度相似组</span></div>
        <div><strong>{scan.sameSiteGroupCount}</strong><span>同站页面组</span></div>
        <div data-danger={scan.selectedDeleteCount > 0 || undefined}>
          <strong>{scan.selectedDeleteCount}</strong><span>待清理网站</span>
        </div>
      </section>

      <div className="similarity-controls">
        <div className="similarity-filter" role="tablist" aria-label="相似网站类型">
          {filters.map((item) => (
            <button
              key={item.value}
              type="button"
              role="tab"
              aria-selected={filter === item.value}
              data-active={filter === item.value || undefined}
              disabled={busy}
              onClick={() => {
                if (busy || item.value === filter) return;
                setGroupsLoading(true);
                setError(null);
                setFilter(item.value);
                setConfirming(false);
                resetPaging();
              }}
            >
              {item.label}<span>{item.count}</span>
            </button>
          ))}
        </div>
        <div className="similarity-control-actions">
          <button
            className="library-button secondary"
            type="button"
            onClick={() => void chooseRecommendedForFilter()}
            disabled={busy || !activeFilter || activeFilter.count === 0}
            title={`选择当前“${activeFilter?.label ?? "全部"}”分区的全部推荐网站`}
          >
            {bulkDecisionBusy ? <Spinner /> : <Sparkles aria-hidden="true" />}
            {bulkDecisionBusy
              ? "正在选择"
              : `一键选择${activeFilter?.label ?? "全部"}推荐（${activeFilter?.count ?? 0} 组）`}
          </button>
          <button className="library-button secondary" type="button" onClick={() => void beginScan()} disabled={busy}>
            <RefreshCw aria-hidden="true" />重新扫描
          </button>
        </div>
      </div>

      {error && (
        <div className="library-error-banner" role="alert">
          <AlertCircle aria-hidden="true" />
          <span>{error}</span>
          <button type="button" onClick={() => void reloadActive()} disabled={scanBusy}>
            <RefreshCw aria-hidden="true" />刷新结果
          </button>
        </div>
      )}

      {scan.groupCount === 0 ? (
        <section className="similarity-empty">
          <ShieldCheck aria-hidden="true" />
          <h3>没有发现需要审阅的网站</h3>
          <p>当前规则下未找到重复变体或同站多页面。</p>
        </section>
      ) : groupsLoading ? (
        <div className="similarity-loading" role="status"><Spinner /><span>正在加载分组</span></div>
      ) : groups.length === 0 ? (
        <section className="similarity-empty">
          <Layers3 aria-hidden="true" />
          <h3>这个分类没有分组</h3>
        </section>
      ) : (
        <div className="similarity-groups">
          {groups.map((group) => {
            const keepAction = siteSimilarityKeepAction(group, group.keepSiteIds);
            return (
              <section className="similarity-group" key={group.id}>
                <header className="similarity-group-header">
                  <div>
                    <span className={`similarity-kind similarity-kind--${group.kind}`}>
                      {groupLabel(group.kind)}
                    </span>
                    <strong>{group.displayHost}</strong>
                    <small>{group.memberCount} 个网站</small>
                  </div>
                  <button
                    type="button"
                    className="similarity-keep-all"
                    data-active="true"
                    disabled={busy}
                    onClick={() => void saveKeepSelection(group, keepAction.keepSiteIds)}
                  >
                    <Check aria-hidden="true" />
                    {keepAction.label}
                  </button>
                </header>
                <div className="similarity-member-grid">
                  {group.members.map((member) => {
                    const selected = group.keepSiteIds.includes(member.id);
                    const willDelete = group.keepSiteIds.length > 0 && !selected;
                    const summary = librarySiteCardSummary(member);
                    const nextKeepSiteIds = toggleSiteSimilarityKeepSelection(
                      group,
                      group.keepSiteIds,
                      member.id,
                    );
                    return (
                      <button
                        type="button"
                        className="similarity-member"
                        key={member.id}
                        data-selected={selected || undefined}
                        data-delete={willDelete || undefined}
                        aria-pressed={selected}
                        disabled={busy}
                        onClick={() => void saveKeepSelection(group, nextKeepSiteIds)}
                      >
                        <span className="similarity-member-topline">
                          <SiteFavicon url={member.faviconUrl} name={member.name} size={32} />
                          <span className="similarity-member-title">
                            <strong>{member.name}</strong>
                            <small title={member.identityUrl}>{displayUrl(member.identityUrl)}</small>
                          </span>
                          {member.isRecommended && <span className="similarity-recommended">推荐</span>}
                        </span>
                        {summary && <span className="similarity-member-summary">{summary}</span>}
                        <span className="similarity-member-taxonomy">
                          <span>{member.category.name}</span>
                          {member.tags.slice(0, 3).map((tag) => <span key={tag.id}>#{tag.name}</span>)}
                        </span>
                        <span className="similarity-member-choice">
                          {selected
                            ? <><Check aria-hidden="true" />将保留</>
                            : willDelete
                              ? <><Trash2 aria-hidden="true" />将清理</>
                              : "点击选择保留"}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </section>
            );
          })}
        </div>
      )}

      {pageCount > 1 && (
        <nav className="similarity-pagination" aria-label="相似网站分组分页">
          <button
            className="icon-button"
            type="button"
            aria-label="上一页"
            title="上一页"
            disabled={busy || page === 1}
            onClick={() => goToPage(page - 1)}
          >
            <ChevronLeft aria-hidden="true" />
          </button>
          <div className="similarity-page-numbers">
            {pageTokens.map((token) => typeof token === "number" ? (
              <button
                key={token}
                className="similarity-page-number"
                type="button"
                aria-label={`第 ${token} 页`}
                aria-current={token === page ? "page" : undefined}
                data-active={token === page || undefined}
                disabled={busy}
                onClick={() => goToPage(token)}
              >
                {token}
              </button>
            ) : (
              <span className="similarity-page-ellipsis" key={token} aria-hidden="true">…</span>
            ))}
          </div>
          <span className="similarity-page-total">共 {pageCount} 页</span>
          <button
            className="icon-button"
            type="button"
            aria-label="下一页"
            title="下一页"
            disabled={busy || page === pageCount}
            onClick={() => goToPage(page + 1)}
          >
            <ChevronRight aria-hidden="true" />
          </button>
        </nav>
      )}

      <footer className="similarity-footer" aria-live="polite">
        <div>
          <strong>{scan.selectedGroupCount > 0 ? `已选择 ${scan.selectedGroupCount} 组` : "尚未选择清理项"}</strong>
          <span>{scan.selectedDeleteCount > 0 ? `确认后将清理 ${scan.selectedDeleteCount} 个网站` : "未选择的分组会完整保留"}</span>
        </div>
        {confirming ? (
          <div className="similarity-confirm-actions">
            <button className="library-button secondary" type="button" onClick={() => setConfirming(false)} disabled={applyBusy}>
              返回审阅
            </button>
            <button className="library-button danger" type="button" onClick={() => void applySelection()} disabled={applyBusy}>
              {applyBusy ? <Spinner /> : <Trash2 aria-hidden="true" />}
              {applyBusy ? "正在清理" : `确认清理 ${scan.selectedDeleteCount} 个`}
            </button>
          </div>
        ) : (
          <button
            className="library-button danger"
            type="button"
            disabled={busy || scan.selectedDeleteCount < 1}
            onClick={() => setConfirming(true)}
          >
            <Trash2 aria-hidden="true" />执行清理
          </button>
        )}
      </footer>
    </div>
  );
}
