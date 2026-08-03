"use client";

import {
  AlertTriangle,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  FileUp,
  ListChecks,
  TableCellsMerge,
  Upload,
} from "lucide-react";
import Link from "next/link";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
} from "react";

import { Spinner } from "@/components/react-bits/spinner";
import { agentResultPageTokens } from "@/lib/agent-pagination";
import {
  applyBookmarkImport,
  BookmarkApiError,
  getBookmarkImportStatus,
  getBookmarkPreviewSummary,
  getBookmarkSimilarityClusters,
  getBookmarkSimilarityMembers,
  keepOriginalBookmarkSimilarityClusters,
  newBookmarkIdempotencyKey,
  setBookmarkSimilarityDecision,
  uploadBookmarkFile,
} from "@/lib/bookmark-client";
import {
  bookmarkCanonicalSourceLabel,
  bookmarkFailureLabel,
  bookmarkImportStateLabel,
  bookmarkSimilarityConfidenceLabel,
  bookmarkSimilarityReasonLabel,
  isBookmarkImportPending,
  isBookmarkImportPreviewReady,
  type BookmarkImportResult,
  type BookmarkImportStatus,
  type BookmarkPreviewSummary,
  type BookmarkSimilarityCluster,
  type BookmarkSimilarityClusterPage,
  type BookmarkSimilarityDecision,
  type BookmarkSimilarityDecisionResult,
  type BookmarkSimilarityMember,
} from "@/lib/bookmark-contract";
import {
  bookmarkFileValidationError,
  isFileDrag,
  selectDroppedBookmarkFile,
} from "@/lib/bookmark-import-drop";

const POLL_INTERVAL_MS = 800;
const MAX_POLLS = 150;

function errorText(error: unknown, fallback: string): string {
  if (error instanceof BookmarkApiError) return error.message;
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

function Stat({ label, value }: Readonly<{ label: string; value: number }>) {
  return (
    <div className="bookmark-stat">
      <dt>{label}</dt>
      <dd>{value.toLocaleString("zh-CN")}</dd>
    </div>
  );
}

function decisionLabel(decision: BookmarkSimilarityDecision | null): string {
  if (decision === "merge_to_homepage") return "将合并到推荐主页";
  if (decision === "keep_originals") return "将保留原样";
  return "待你决定";
}

function MemberRows({ members }: Readonly<{ members: BookmarkSimilarityMember[] }>) {
  return (
    <ul className="bookmark-similarity-members">
      {members.map((member) => (
        <li key={member.candidateId}>
          <div className="bookmark-similarity-member-copy">
            <strong>{member.title}</strong>
            <span title={member.displayUrl}>{member.displayUrl}</span>
          </div>
          <div className="bookmark-similarity-member-meta">
            {member.isCanonical && <span>推荐主页</span>}
            {member.occurrenceCount > 1 && <span>出现 {member.occurrenceCount} 次</span>}
          </div>
        </li>
      ))}
    </ul>
  );
}

type SimilarityClusterCardProps = {
  jobId: string;
  cluster: BookmarkSimilarityCluster;
  decisionVersion: number;
  decisionBusy: boolean;
  onDecision: (clusterId: string, decision: BookmarkSimilarityDecision) => void;
};

function SimilarityClusterCard({
  jobId,
  cluster,
  decisionVersion,
  decisionBusy,
  onDecision,
}: Readonly<SimilarityClusterCardProps>) {
  const [expanded, setExpanded] = useState(false);
  const [members, setMembers] = useState(cluster.sampleMembers);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadedFullPage, setLoadedFullPage] = useState(false);
  const [loadingMembers, setLoadingMembers] = useState(false);
  const [membersError, setMembersError] = useState<string | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const requestGenerationRef = useRef(0);

  useEffect(() => () => {
    requestGenerationRef.current += 1;
    requestRef.current?.abort();
  }, []);

  const loadMembers = async (cursor: string | null, append: boolean) => {
    if (loadingMembers) return;
    requestRef.current?.abort();
    const controller = new AbortController();
    const generation = requestGenerationRef.current + 1;
    requestGenerationRef.current = generation;
    requestRef.current = controller;
    setLoadingMembers(true);
    setMembersError(null);
    try {
      const page = await getBookmarkSimilarityMembers(
        jobId,
        cluster.id,
        cursor ? { cursor } : {},
        controller.signal,
      );
      if (controller.signal.aborted || requestGenerationRef.current !== generation) return;
      if (page.decisionVersion !== decisionVersion) {
        setMembersError("相似组选择已更新，请收起后重新查看");
        return;
      }
      setMembers((current) => {
        const combined = append ? [...current, ...page.items] : page.items;
        return Array.from(
          new Map(combined.map((member) => [member.candidateId, member])).values(),
        );
      });
      setNextCursor(page.nextCursor);
      setLoadedFullPage(true);
    } catch (failure) {
      if (!isAbortError(failure)) {
        setMembersError(errorText(failure, "页面明细加载失败，请重试"));
      }
    } finally {
      if (requestGenerationRef.current === generation) {
        requestRef.current = null;
        setLoadingMembers(false);
      }
    }
  };

  const toggleMembers = () => {
    if (expanded) {
      requestRef.current?.abort();
      setExpanded(false);
      return;
    }
    setExpanded(true);
    if (cluster.hasMoreMembers && !loadedFullPage) void loadMembers(null, false);
  };

  const reasonLabels = Array.from(
    new Set(cluster.reasonCodes.map(bookmarkSimilarityReasonLabel)),
  ).slice(0, 4);

  return (
    <article className="bookmark-similarity-cluster" data-decision={cluster.decision ?? "unresolved"}>
      <header className="bookmark-similarity-cluster-head">
        <div className="bookmark-similarity-cluster-title">
          <strong>{cluster.displayHost}</strong>
          <span className="bookmark-similarity-confidence" data-level={cluster.confidence}>
            {bookmarkSimilarityConfidenceLabel(cluster.confidence)}
          </span>
          <span className="bookmark-similarity-decision">
            {decisionLabel(cluster.decision)}
          </span>
        </div>
        <span className="bookmark-similarity-count">
          {cluster.candidateCount.toLocaleString("zh-CN")} 个页面
          {cluster.occurrenceCount !== cluster.candidateCount && (
            <>，共出现 {cluster.occurrenceCount.toLocaleString("zh-CN")} 次</>
          )}
        </span>
      </header>

      <div className="bookmark-similarity-reasons" aria-label="判定依据">
        {reasonLabels.map((label) => <span key={label}>{label}</span>)}
      </div>

      <div className="bookmark-similarity-canonical">
        <span>一键合并后保留</span>
        <strong>{cluster.canonical.title}</strong>
        <span title={cluster.canonical.url}>{cluster.canonical.url}</span>
        <small>{bookmarkCanonicalSourceLabel(cluster.canonical.source)}</small>
      </div>

      <MemberRows members={expanded ? members : cluster.sampleMembers} />

      {(cluster.hasMoreMembers || loadedFullPage) && (
        <div className="bookmark-similarity-member-actions">
          <button
            className="bookmark-inline-button"
            type="button"
            aria-expanded={expanded}
            onClick={toggleMembers}
          >
            {expanded ? <ChevronUp aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
            {expanded ? "收起页面明细" : `查看全部 ${cluster.candidateCount} 个页面`}
          </button>
          {expanded && nextCursor && (
            <button
              className="bookmark-inline-button"
              type="button"
              disabled={loadingMembers}
              onClick={() => void loadMembers(nextCursor, true)}
            >
              {loadingMembers && <Spinner size={13} />}
              {loadingMembers ? "正在加载" : "加载更多页面"}
            </button>
          )}
        </div>
      )}
      {expanded && loadingMembers && !loadedFullPage && (
        <p className="bookmark-similarity-member-status" role="status">
          <Spinner size={13} />正在加载全部页面
        </p>
      )}
      {membersError && (
        <p className="bookmark-similarity-member-error" role="alert">{membersError}</p>
      )}

      <div className="bookmark-similarity-actions" role="group" aria-label={`${cluster.displayHost} 入库方式`}>
        <button
          className="provider-btn provider-btn-secondary provider-btn-sm"
          type="button"
          aria-pressed={cluster.decision === "merge_to_homepage"}
          data-selected={cluster.decision === "merge_to_homepage" || undefined}
          disabled={decisionBusy}
          onClick={() => onDecision(cluster.id, "merge_to_homepage")}
        >
          <TableCellsMerge aria-hidden="true" />
          一键合并
        </button>
        <button
          className="provider-btn provider-btn-secondary provider-btn-sm"
          type="button"
          aria-pressed={cluster.decision === "keep_originals"}
          data-selected={cluster.decision === "keep_originals" || undefined}
          disabled={decisionBusy}
          onClick={() => onDecision(cluster.id, "keep_originals")}
        >
          <ListChecks aria-hidden="true" />
          保留原样入库
        </button>
      </div>
    </article>
  );
}

function updatedPreview(
  preview: BookmarkPreviewSummary,
  result: BookmarkSimilarityDecisionResult,
): BookmarkPreviewSummary {
  if (preview.jobId !== result.jobId || preview.runId !== result.runId) return preview;
  return {
    ...preview,
    jobVersion: result.jobVersion,
    decisionVersion: result.decisionVersion,
    similarityDecisions: result.similarityDecisions,
    selectedMergeReductionCount: result.selectedMergeReductionCount,
    projectedCreateCount: result.projectedCreateCount,
  };
}

export function BookmarkImportWorkspace() {
  const [status, setStatus] = useState<BookmarkImportStatus | null>(null);
  const [preview, setPreview] = useState<BookmarkPreviewSummary | null>(null);
  const [result, setResult] = useState<BookmarkImportResult | null>(null);
  const [clusterPage, setClusterPage] = useState<BookmarkSimilarityClusterPage | null>(null);
  const [clustersLoading, setClustersLoading] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [decisionBusy, setDecisionBusy] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [fileDragActive, setFileDragActive] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const idempotencyKeyRef = useRef<string | null>(null);
  const generationRef = useRef(0);
  const previewRef = useRef<BookmarkPreviewSummary | null>(null);
  const pollingControllerRef = useRef<AbortController | null>(null);
  const clusterControllerRef = useRef<AbortController | null>(null);
  const decisionInFlightRef = useRef(false);
  const uploadInFlightRef = useRef(false);
  const applyInFlightRef = useRef(false);
  const fileDragDepthRef = useRef(0);

  useEffect(() => {
    previewRef.current = preview;
  }, [preview]);

  useEffect(() => () => {
    generationRef.current += 1;
    pollingControllerRef.current?.abort();
    clusterControllerRef.current?.abort();
  }, []);

  const publishPreview = useCallback(
    (summary: BookmarkPreviewSummary, page: BookmarkSimilarityClusterPage | null) => {
      previewRef.current = summary;
      setPreview(summary);
      setClusterPage(page);
      setReviewError(null);
    },
    [],
  );

  const loadReadyPreview = useCallback(async (
    jobId: string,
    generation: number,
    signal: AbortSignal,
  ) => {
    let summary = await getBookmarkPreviewSummary(jobId, signal);
    let page = summary.similarityClusterCount > 0
      ? await getBookmarkSimilarityClusters(jobId, {}, signal)
      : null;
    if (page && page.decisionVersion !== summary.decisionVersion) {
      summary = await getBookmarkPreviewSummary(jobId, signal);
      page = await getBookmarkSimilarityClusters(jobId, {}, signal);
    }
    if (signal.aborted || generationRef.current !== generation) return;
    publishPreview(summary, page);
  }, [publishPreview]);

  const pollUntilReady = useCallback(async (jobId: string, generation: number) => {
    pollingControllerRef.current?.abort();
    const controller = new AbortController();
    pollingControllerRef.current = controller;
    for (let attempt = 0; attempt < MAX_POLLS; attempt += 1) {
      if (controller.signal.aborted || generationRef.current !== generation) return;
      const next = await getBookmarkImportStatus(jobId, controller.signal);
      if (controller.signal.aborted || generationRef.current !== generation) return;
      setStatus(next);
      if (next.state === "failed") {
        setError(bookmarkFailureLabel(next.failureCode) ?? "解析失败，请重试");
        return;
      }
      if (isBookmarkImportPreviewReady(next.state)) {
        await loadReadyPreview(jobId, generation, controller.signal);
        return;
      }
      if (!isBookmarkImportPending(next.state)) return;
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    }
    if (!controller.signal.aborted && generationRef.current === generation) {
      setError("解析用时过长，请刷新页面查看任务状态");
    }
  }, [loadReadyPreview]);

  const processFile = useCallback(async (file: File) => {
    if (
      uploadInFlightRef.current
      || applyInFlightRef.current
      || decisionInFlightRef.current
    ) {
      setError("当前任务正在处理，完成后可再次上传");
      return;
    }
    const validationError = bookmarkFileValidationError(file);
    if (validationError) {
      setError(validationError);
      return;
    }

    uploadInFlightRef.current = true;
    generationRef.current += 1;
    const generation = generationRef.current;
    pollingControllerRef.current?.abort();
    clusterControllerRef.current?.abort();
    setError(null);
    setReviewError(null);
    setPreview(null);
    previewRef.current = null;
    setClusterPage(null);
    setResult(null);
    setStatus(null);
    setFileName(file.name);
    setUploading(true);
    idempotencyKeyRef.current = newBookmarkIdempotencyKey();
    try {
      const upload = await uploadBookmarkFile(file, idempotencyKeyRef.current);
      if (generationRef.current !== generation) return;
      await pollUntilReady(upload.jobId, generation);
    } catch (failure) {
      if (!isAbortError(failure) && generationRef.current === generation) {
        setError(errorText(failure, "上传失败，请重试"));
      }
    } finally {
      uploadInFlightRef.current = false;
      if (generationRef.current === generation) setUploading(false);
    }
  }, [pollUntilReady]);

  const handleFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) void processFile(file);
  };

  useEffect(() => {
    const resetFileDrag = () => {
      fileDragDepthRef.current = 0;
      setFileDragActive(false);
    };

    const handleDragEnter = (event: DragEvent) => {
      if (!isFileDrag(event.dataTransfer)) return;
      event.preventDefault();
      fileDragDepthRef.current += 1;
      setFileDragActive(true);
    };

    const handleDragOver = (event: DragEvent) => {
      if (!isFileDrag(event.dataTransfer)) return;
      event.preventDefault();
      if (event.dataTransfer) {
        const busy = uploadInFlightRef.current
          || applyInFlightRef.current
          || decisionInFlightRef.current;
        event.dataTransfer.dropEffect = busy ? "none" : "copy";
      }
    };

    const handleDragLeave = (event: DragEvent) => {
      if (fileDragDepthRef.current === 0) return;
      event.preventDefault();
      fileDragDepthRef.current = Math.max(0, fileDragDepthRef.current - 1);
      if (fileDragDepthRef.current === 0) setFileDragActive(false);
    };

    const handleDrop = (event: DragEvent) => {
      if (!isFileDrag(event.dataTransfer)) return;
      event.preventDefault();
      resetFileDrag();
      if (
        uploadInFlightRef.current
        || applyInFlightRef.current
        || decisionInFlightRef.current
      ) {
        setError("当前任务正在处理，完成后可再次上传");
        return;
      }
      const selection = selectDroppedBookmarkFile(event.dataTransfer?.files ?? []);
      if (!selection.ok) {
        setError(selection.error);
        return;
      }
      void processFile(selection.file);
    };

    window.addEventListener("dragenter", handleDragEnter, true);
    window.addEventListener("dragover", handleDragOver, true);
    window.addEventListener("dragleave", handleDragLeave, true);
    window.addEventListener("drop", handleDrop, true);
    window.addEventListener("dragend", resetFileDrag, true);
    return () => {
      window.removeEventListener("dragenter", handleDragEnter, true);
      window.removeEventListener("dragover", handleDragOver, true);
      window.removeEventListener("dragleave", handleDragLeave, true);
      window.removeEventListener("drop", handleDrop, true);
      window.removeEventListener("dragend", resetFileDrag, true);
    };
  }, [processFile]);

  const loadClusterPage = useCallback(async (pageNumber: number) => {
    const currentPreview = previewRef.current;
    if (!currentPreview || clustersLoading) return;
    const generation = generationRef.current;
    clusterControllerRef.current?.abort();
    const controller = new AbortController();
    clusterControllerRef.current = controller;
    setClustersLoading(true);
    setReviewError(null);
    try {
      const page = await getBookmarkSimilarityClusters(
        currentPreview.jobId,
        { page: pageNumber },
        controller.signal,
      );
      if (controller.signal.aborted || generationRef.current !== generation) return;
      if (page.decisionVersion !== previewRef.current?.decisionVersion) {
        const freshSummary = await getBookmarkPreviewSummary(
          currentPreview.jobId,
          controller.signal,
        );
        if (controller.signal.aborted || generationRef.current !== generation) return;
        previewRef.current = freshSummary;
        setPreview(freshSummary);
      }
      setClusterPage(page);
    } catch (failure) {
      if (!isAbortError(failure)) {
        setReviewError(errorText(failure, "相似组加载失败，请重试"));
      }
    } finally {
      if (clusterControllerRef.current === controller) {
        clusterControllerRef.current = null;
        setClustersLoading(false);
      }
    }
  }, [clustersLoading]);

  const goToClusterPage = (pageNumber: number) => {
    if (
      !clusterPage
      || pageNumber === clusterPage.page
      || pageNumber < 1
      || pageNumber > clusterPage.totalPages
    ) return;
    void loadClusterPage(pageNumber);
  };

  const goToPreviousClusterPage = () => {
    if (!clusterPage || clusterPage.page <= 1) return;
    goToClusterPage(clusterPage.page - 1);
  };

  const goToNextClusterPage = () => {
    if (!clusterPage || clusterPage.page >= clusterPage.totalPages) return;
    goToClusterPage(clusterPage.page + 1);
  };

  const applyDecisionState = (decisionResult: BookmarkSimilarityDecisionResult) => {
    const current = previewRef.current;
    if (!current) return;
    const next = updatedPreview(current, decisionResult);
    previewRef.current = next;
    setPreview(next);
  };

  const handleDecision = async (
    clusterId: string,
    decision: BookmarkSimilarityDecision,
  ) => {
    const current = previewRef.current;
    if (!current || decisionInFlightRef.current) return;
    const selectedCluster = clusterPage?.items.find((cluster) => cluster.id === clusterId);
    if (selectedCluster?.decision === decision) return;
    decisionInFlightRef.current = true;
    const generation = generationRef.current;
    setDecisionBusy(clusterId);
    setReviewError(null);
    try {
      const decisionResult = await setBookmarkSimilarityDecision(current.jobId, clusterId, {
        expectedJobVersion: current.jobVersion,
        expectedDecisionVersion: current.decisionVersion,
        decision,
      });
      if (generationRef.current !== generation) return;
      applyDecisionState(decisionResult);
      setClusterPage((page) => page ? {
        ...page,
        decisionVersion: decisionResult.decisionVersion,
        items: page.items.map((cluster) =>
          cluster.id === clusterId ? { ...cluster, decision } : cluster,
        ),
      } : page);
    } catch (failure) {
      if (!isAbortError(failure) && generationRef.current === generation) {
        setReviewError(errorText(failure, "保存相似组选择失败，请重试"));
      }
    } finally {
      if (generationRef.current === generation) setDecisionBusy(null);
      decisionInFlightRef.current = false;
    }
  };

  const handleKeepAllOriginals = async () => {
    const current = previewRef.current;
    if (
      !current
      || current.similarityDecisions.unresolved === 0
      || decisionInFlightRef.current
    ) return;
    decisionInFlightRef.current = true;
    const generation = generationRef.current;
    setDecisionBusy("all");
    setReviewError(null);
    try {
      const decisionResult = await keepOriginalBookmarkSimilarityClusters(current.jobId, {
        expectedJobVersion: current.jobVersion,
        expectedDecisionVersion: current.decisionVersion,
      });
      if (generationRef.current !== generation) return;
      applyDecisionState(decisionResult);
      setClusterPage((page) => page ? {
        ...page,
        decisionVersion: decisionResult.decisionVersion,
        items: page.items.map((cluster) =>
          cluster.decision === null ? { ...cluster, decision: "keep_originals" } : cluster,
        ),
      } : page);
    } catch (failure) {
      if (!isAbortError(failure) && generationRef.current === generation) {
        setReviewError(errorText(failure, "批量保存相似组选择失败，请重试"));
      }
    } finally {
      if (generationRef.current === generation) setDecisionBusy(null);
      decisionInFlightRef.current = false;
    }
  };

  const handleApply = async () => {
    const current = previewRef.current;
    if (
      !current
      || current.similarityDecisions.unresolved > 0
      || applyInFlightRef.current
      || decisionInFlightRef.current
    ) return;
    const generation = generationRef.current;
    applyInFlightRef.current = true;
    setApplying(true);
    setError(null);
    try {
      const applied = await applyBookmarkImport(
        current.jobId,
        current.jobVersion,
        current.decisionVersion,
      );
      if (generationRef.current === generation) setResult(applied);
    } catch (failure) {
      if (generationRef.current === generation) {
        setError(errorText(failure, "导入失败，请重试"));
      }
    } finally {
      applyInFlightRef.current = false;
      if (generationRef.current === generation) setApplying(false);
    }
  };

  const willSkip = preview ? preview.actions.reject + preview.actions.needsReview : 0;
  const unresolved = preview?.similarityDecisions.unresolved ?? 0;
  const clusterPageTokens = clusterPage
    ? agentResultPageTokens(clusterPage.totalPages, clusterPage.page)
    : [];

  return (
    <div className="provider-page">
      {fileDragActive && (
        <div
          className="bookmark-file-drop-overlay"
          data-busy={uploading || applying || decisionBusy !== null}
          role="status"
          aria-live="polite"
        >
          <div className="bookmark-file-drop-target">
            <FileUp aria-hidden="true" />
            <strong>
              {uploading || applying || decisionBusy !== null
                ? "当前任务正在处理"
                : "松开以上传书签文件"}
            </strong>
            <span>
              {uploading || applying || decisionBusy !== null
                ? "完成后可再次上传"
                : "支持 Chrome、Edge、Firefox、Safari 导出的 HTML 文件"}
            </span>
          </div>
        </div>
      )}
      {error && (
        <p className="provider-error" role="alert">
          <AlertTriangle aria-hidden="true" />
          <span>{error}</span>
        </p>
      )}

      <section className="provider-section" aria-label="上传书签文件">
        <div className="provider-section-head">
          <div className="provider-section-titles">
            <h2 className="provider-section-title">
              <FileUp aria-hidden="true" />
              选择书签文件
            </h2>
            <p className="provider-section-desc">
              浏览器里导出的书签 HTML 文件（Chrome / Edge / Firefox / Safari 都支持）。
              上传只做解析与统计，<strong>不会写入网址库</strong>，要不要导入由你在下一步决定。
            </p>
          </div>
          <button
            className="provider-btn provider-btn-primary"
            type="button"
            disabled={uploading || applying || decisionBusy !== null}
            onClick={() => inputRef.current?.click()}
          >
            {uploading ? <Spinner size={14} /> : <Upload aria-hidden="true" />}
            {uploading ? "正在上传" : "选择文件"}
          </button>
        </div>
        <input
          ref={inputRef}
          className="sr-only"
          type="file"
          accept=".html,.htm,text/html"
          disabled={uploading || applying || decisionBusy !== null}
          onChange={(event) => void handleFile(event)}
        />
        {fileName && <p className="provider-form-hint">已选择：{fileName}</p>}
      </section>

      {status && !preview && !result && (
        <p className="provider-loading" aria-busy={isBookmarkImportPending(status.state)}>
          {isBookmarkImportPending(status.state) && <Spinner size={14} />}
          {bookmarkImportStateLabel(status.state)}
          {status.progress.total > 0 && `（${status.progress.completed} / ${status.progress.total}）`}
        </p>
      )}

      {preview && !result && (
        <section className="provider-section" aria-label="解析结果">
          <div className="provider-section-titles">
            <h2 className="provider-section-title">解析完成</h2>
            <p className="provider-section-desc">
              以下数字来自这份文件本身，此时网址库还没有任何变化。
            </p>
          </div>
          <dl className="bookmark-stats">
            <Stat label="文件夹" value={preview.folderCount} />
            <Stat label="书签条目" value={preview.occurrenceCount} />
            <Stat label="去重后" value={preview.candidateCount} />
            <Stat label="精确重复" value={preview.duplicateOccurrenceCount} />
            <Stat label="相似站点组" value={preview.similarityClusterCount} />
            <Stat label="预计新增" value={preview.projectedCreateCount} />
          </dl>

          {preview.similarityClusterCount > 0 && (
            <div className="bookmark-similarity-review" aria-labelledby="bookmark-similarity-title">
              <div className="bookmark-similarity-review-head">
                <div>
                  <h3 id="bookmark-similarity-title">
                    <TableCellsMerge aria-hidden="true" />
                    处理同站点的不同页面
                  </h3>
                  <p>
                    共 {preview.similarityClusterCount.toLocaleString("zh-CN")} 组，涉及
                    {" "}{preview.similarityCandidateCount.toLocaleString("zh-CN")} 个网址；
                    还有 <strong>{unresolved.toLocaleString("zh-CN")}</strong> 组待决定。
                  </p>
                </div>
                <button
                  className="provider-btn provider-btn-secondary provider-btn-sm"
                  type="button"
                  disabled={unresolved === 0 || decisionBusy !== null}
                  onClick={() => void handleKeepAllOriginals()}
                >
                  {decisionBusy === "all" ? <Spinner size={14} /> : <CheckCheck aria-hidden="true" />}
                  {decisionBusy === "all"
                    ? "正在保存"
                    : `全部未决保留原样（${unresolved.toLocaleString("zh-CN")}）`}
                </button>
              </div>

              <div className="bookmark-similarity-summary" aria-live="polite">
                <span>合并 {preview.similarityDecisions.mergeToHomepage} 组</span>
                <span>原样保留 {preview.similarityDecisions.keepOriginals} 组</span>
                <span>预计减少 {preview.selectedMergeReductionCount} 条</span>
              </div>

              {reviewError && (
                <p className="bookmark-similarity-review-error" role="alert">
                  <AlertTriangle aria-hidden="true" />{reviewError}
                </p>
              )}

              {clustersLoading && !clusterPage && (
                <p className="bookmark-similarity-loading" role="status">
                  <Spinner size={14} />正在加载相似组
                </p>
              )}

              {clusterPage && (
                <div className="bookmark-similarity-list" aria-busy={clustersLoading}>
                  {clusterPage.items.map((cluster) => (
                    <SimilarityClusterCard
                      key={cluster.id}
                      jobId={preview.jobId}
                      cluster={cluster}
                      decisionVersion={clusterPage.decisionVersion}
                      decisionBusy={decisionBusy !== null}
                      onDecision={(clusterId, decision) => void handleDecision(clusterId, decision)}
                    />
                  ))}
                </div>
              )}

              {clusterPage && (
                <nav className="bookmark-similarity-pagination" aria-label="相似书签组分页">
                  <button
                    type="button"
                    aria-label="上一页"
                    title="上一页"
                    disabled={clusterPage.page === 1 || clustersLoading || decisionBusy !== null}
                    onClick={goToPreviousClusterPage}
                  >
                    <ChevronLeft aria-hidden="true" />
                  </button>
                  <div className="bookmark-similarity-page-numbers">
                    {clusterPageTokens.map((token) => typeof token === "number" ? (
                      <button
                        type="button"
                        key={token}
                        aria-label={`第 ${token} 页`}
                        aria-current={token === clusterPage.page ? "page" : undefined}
                        data-active={token === clusterPage.page || undefined}
                        disabled={clustersLoading || decisionBusy !== null}
                        onClick={() => goToClusterPage(token)}
                      >
                        {token}
                      </button>
                    ) : (
                      <span
                        className="bookmark-similarity-page-ellipsis"
                        key={token}
                        aria-hidden="true"
                      >
                        …
                      </span>
                    ))}
                  </div>
                  <span className="bookmark-similarity-page-total" aria-live="polite">
                    共 {clusterPage.totalPages} 页
                  </span>
                  <button
                    type="button"
                    aria-label="下一页"
                    title="下一页"
                    disabled={
                      clusterPage.page >= clusterPage.totalPages
                      || clustersLoading
                      || decisionBusy !== null
                    }
                    onClick={goToNextClusterPage}
                  >
                    <ChevronRight aria-hidden="true" />
                  </button>
                </nav>
              )}
            </div>
          )}

          <p className="provider-form-hint">
            预计新增 <strong>{preview.projectedCreateCount.toLocaleString("zh-CN")}</strong> 条。
            已存在的网址会自动跳过，不会产生重复
            {willSkip > 0 && <>；另有 {willSkip.toLocaleString("zh-CN")} 条需人工确认，本次不会导入</>}。
            {preview.sensitiveCandidateCount > 0 && (
              <>
                {" "}其中 {preview.sensitiveCandidateCount} 条网址带有疑似敏感参数，
                预览只显示脱敏地址，原始数据不会被访问。
              </>
            )}
          </p>
          {unresolved > 0 && (
            <p className="bookmark-apply-blocker" role="status">
              <AlertTriangle aria-hidden="true" />
              还需处理 {unresolved.toLocaleString("zh-CN")} 组相似书签，完成后才能确认导入。
            </p>
          )}
          <div className="provider-form-actions">
            <button
              className="provider-btn provider-btn-primary"
              type="button"
              disabled={applying || unresolved > 0 || decisionBusy !== null}
              onClick={() => void handleApply()}
            >
              {applying && <Spinner size={14} />}
              {applying
                ? "正在导入"
                : `确认导入 ${preview.projectedCreateCount.toLocaleString("zh-CN")} 条`}
            </button>
          </div>
        </section>
      )}

      {result && (
        <section className="provider-section" aria-label="导入结果">
          <p className="provider-test-result" data-status="ok" role="status">
            <Check aria-hidden="true" />
            导入完成：新增 {result.created.toLocaleString("zh-CN")} 条
            {result.mergedCandidates > 0 && `，合并清理 ${result.mergedCandidates.toLocaleString("zh-CN")} 条`}
            {result.skippedExisting > 0 && `，跳过已存在 ${result.skippedExisting.toLocaleString("zh-CN")} 条`}
            {result.skippedNeedsReview > 0 && `，${result.skippedNeedsReview} 条需人工确认未导入`}
            {result.failed > 0 && `，${result.failed} 条失败`}
          </p>
          <p className="provider-form-hint">
            去 <Link href="/library">网址库</Link> 看看，或者回 <Link href="/">首页</Link> 让 Agent
            帮你继续整理。
          </p>
        </section>
      )}
    </div>
  );
}
