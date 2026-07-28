"use client";

import { AlertTriangle, Check, FileUp, Upload } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type ChangeEvent } from "react";

import { Spinner } from "@/components/react-bits/spinner";
import {
  applyBookmarkImport,
  BookmarkApiError,
  getBookmarkImportStatus,
  getBookmarkPreviewSummary,
  newBookmarkIdempotencyKey,
  uploadBookmarkFile,
} from "@/lib/bookmark-client";
import {
  bookmarkFailureLabel,
  bookmarkImportStateLabel,
  isBookmarkImportPending,
  isBookmarkImportPreviewReady,
  type BookmarkImportResult,
  type BookmarkImportStatus,
  type BookmarkPreviewSummary,
} from "@/lib/bookmark-contract";

const POLL_INTERVAL_MS = 800;
// 解析 2500 条约 1 秒；这个上限只是防止后端卡死时无限轮询下去。
const MAX_POLLS = 150;

function errorText(error: unknown, fallback: string): string {
  if (error instanceof BookmarkApiError) return error.message;
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function Stat({ label, value }: Readonly<{ label: string; value: number }>) {
  return (
    <div className="bookmark-stat">
      <dt>{label}</dt>
      <dd>{value.toLocaleString("zh-CN")}</dd>
    </div>
  );
}

export function BookmarkImportWorkspace() {
  const [status, setStatus] = useState<BookmarkImportStatus | null>(null);
  const [preview, setPreview] = useState<BookmarkPreviewSummary | null>(null);
  const [result, setResult] = useState<BookmarkImportResult | null>(null);
  const [uploading, setUploading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // 每次上传一个新幂等键；重试同一个文件时复用，避免生成第二份快照。
  const idempotencyKeyRef = useRef<string | null>(null);
  const pollingRef = useRef(false);

  useEffect(() => () => {
    pollingRef.current = false;
  }, []);

  const pollUntilReady = useCallback(async (jobId: string) => {
    pollingRef.current = true;
    for (let attempt = 0; attempt < MAX_POLLS; attempt += 1) {
      if (!pollingRef.current) return;
      const next = await getBookmarkImportStatus(jobId);
      setStatus(next);
      if (next.state === "failed") {
        setError(bookmarkFailureLabel(next.failureCode) ?? "解析失败，请重试");
        return;
      }
      if (isBookmarkImportPreviewReady(next.state)) {
        setPreview(await getBookmarkPreviewSummary(jobId));
        return;
      }
      if (!isBookmarkImportPending(next.state)) return;
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    }
    setError("解析用时过长，请刷新页面查看任务状态");
  }, []);

  const handleFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    // 允许重新选择同一个文件：不清空 value 的话第二次不会触发 change。
    event.target.value = "";
    if (!file) return;

    setError(null);
    setPreview(null);
    setResult(null);
    setStatus(null);
    setFileName(file.name);
    setUploading(true);
    idempotencyKeyRef.current = newBookmarkIdempotencyKey();
    try {
      const upload = await uploadBookmarkFile(file, idempotencyKeyRef.current);
      await pollUntilReady(upload.jobId);
    } catch (failure) {
      setError(errorText(failure, "上传失败，请重试"));
    } finally {
      setUploading(false);
    }
  };

  const handleApply = async () => {
    if (!preview || applying) return;
    setApplying(true);
    setError(null);
    try {
      setResult(await applyBookmarkImport(preview.jobId, preview.jobVersion));
    } catch (failure) {
      setError(errorText(failure, "导入失败，请重试"));
    } finally {
      setApplying(false);
    }
  };

  const willImport = preview
    ? preview.actions.create + preview.actions.skipExisting + preview.actions.mergeMissingMetadata
    : 0;
  const willSkip = preview ? preview.actions.reject + preview.actions.needsReview : 0;

  return (
    <div className="provider-page">
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
              <FileUp aria-hidden={true} />
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
            disabled={uploading || applying}
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
            <Stat label="重复合并" value={preview.duplicateOccurrenceCount} />
          </dl>
          <p className="provider-form-hint">
            将导入 <strong>{willImport.toLocaleString("zh-CN")}</strong> 条
            {willSkip > 0 && <>，跳过 {willSkip.toLocaleString("zh-CN")} 条需人工确认的条目</>}
            。已存在的网址会自动跳过，不会产生重复。
            {preview.sensitiveCandidateCount > 0 && (
              <>
                {" "}其中 {preview.sensitiveCandidateCount} 条网址里带有疑似敏感参数，
                会照原样保存，不会被访问。
              </>
            )}
          </p>
          <div className="provider-form-actions">
            <button
              className="provider-btn provider-btn-primary"
              type="button"
              disabled={applying}
              onClick={() => void handleApply()}
            >
              {applying && <Spinner size={14} />}
              {applying ? "正在导入" : `确认导入 ${willImport.toLocaleString("zh-CN")} 条`}
            </button>
          </div>
        </section>
      )}

      {result && (
        <section className="provider-section" aria-label="导入结果">
          <p className="provider-test-result" data-status="ok" role="status">
            <Check aria-hidden="true" />
            导入完成：新增 {result.created.toLocaleString("zh-CN")} 条
            {result.skippedExisting > 0 && `，跳过已存在 ${result.skippedExisting.toLocaleString("zh-CN")} 条`}
            {result.skippedNeedsReview > 0 && `，${result.skippedNeedsReview} 条需人工确认未导入`}
            {result.failed > 0 && `，${result.failed} 条失败`}
          </p>
          <p className="provider-form-hint">
            去 <Link href="/library">网址库</Link> 看看，或者回 <Link href="/">首页</Link> 让 Agent
            帮你整理分类。
          </p>
        </section>
      )}
    </div>
  );
}
