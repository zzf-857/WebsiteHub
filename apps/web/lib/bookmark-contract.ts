// 书签导入的前端契约层。与 provider-contract 同一套约定：
// 归一化失败就抛错，绝不把半截数据放进 state。

export const BOOKMARK_IMPORT_STATES = [
  "receiving",
  "queued_parse",
  "parsing",
  "parse_preview_ready",
  "queued_classification",
  "classifying",
  "final_preview_ready",
  "committing",
  "completed",
  "completed_with_errors",
  "cancel_requested",
  "cancelled",
  "failed",
  "expired",
] as const;
export type BookmarkImportState = (typeof BOOKMARK_IMPORT_STATES)[number];

// 还在推进中的状态：前端据此决定要不要继续轮询。
const PENDING_STATES = new Set<string>([
  "receiving",
  "queued_parse",
  "parsing",
  "queued_classification",
  "classifying",
  "committing",
]);

export function isBookmarkImportPending(state: string): boolean {
  return PENDING_STATES.has(state);
}

export function isBookmarkImportPreviewReady(state: string): boolean {
  return state === "parse_preview_ready" || state === "final_preview_ready";
}

export type BookmarkImportStatus = {
  jobId: string;
  state: string;
  jobVersion: number;
  previewVersion: number;
  progress: { completed: number; total: number };
  failureCode: string | null;
  createdAt: string;
  updatedAt: string;
};

export type BookmarkImportUpload = {
  jobId: string;
  state: string;
  jobVersion: number;
  replayed: boolean;
  sameSourceWarning: boolean;
};

export type BookmarkPreviewSummary = {
  jobId: string;
  runId: string;
  jobVersion: number;
  folderCount: number;
  occurrenceCount: number;
  candidateCount: number;
  duplicateOccurrenceCount: number;
  sensitiveCandidateCount: number;
  actions: {
    create: number;
    skipExisting: number;
    mergeMissingMetadata: number;
    reject: number;
    needsReview: number;
  };
};

export type BookmarkImportResult = {
  jobId: string;
  state: string;
  jobVersion: number;
  totalCandidates: number;
  created: number;
  skippedExisting: number;
  skippedNeedsReview: number;
  failed: number;
};

type JsonRecord = Record<string, unknown>;

export class BookmarkContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BookmarkContractError";
  }
}

function record(value: unknown, path: string): JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new BookmarkContractError(`${path} 必须是对象`);
  }
  return value as JsonRecord;
}

function text(value: unknown, path: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new BookmarkContractError(`${path} 必须是非空字符串`);
  }
  return value.trim();
}

function count(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new BookmarkContractError(`${path} 必须是非负整数`);
  }
  return value as number;
}

function version(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) {
    throw new BookmarkContractError(`${path} 必须是正整数`);
  }
  return value as number;
}

export function normalizeBookmarkImportUpload(value: unknown): BookmarkImportUpload {
  const candidate = record(value, "bookmark_upload");
  return {
    jobId: text(candidate.job_id, "bookmark_upload.job_id"),
    state: text(candidate.state, "bookmark_upload.state"),
    jobVersion: version(candidate.job_version, "bookmark_upload.job_version"),
    replayed: candidate.replayed === true,
    sameSourceWarning: candidate.same_source_warning === true,
  };
}

export function normalizeBookmarkImportStatus(value: unknown): BookmarkImportStatus {
  const candidate = record(value, "bookmark_status");
  const progress = record(candidate.progress, "bookmark_status.progress");
  return {
    jobId: text(candidate.job_id, "bookmark_status.job_id"),
    state: text(candidate.state, "bookmark_status.state"),
    jobVersion: version(candidate.job_version, "bookmark_status.job_version"),
    previewVersion: count(candidate.preview_version, "bookmark_status.preview_version"),
    progress: {
      completed: count(progress.completed, "bookmark_status.progress.completed"),
      total: count(progress.total, "bookmark_status.progress.total"),
    },
    failureCode:
      typeof candidate.failure_code === "string" && candidate.failure_code.trim()
        ? candidate.failure_code.trim()
        : null,
    createdAt: text(candidate.created_at, "bookmark_status.created_at"),
    updatedAt: text(candidate.updated_at, "bookmark_status.updated_at"),
  };
}

export function normalizeBookmarkPreviewSummary(value: unknown): BookmarkPreviewSummary {
  const candidate = record(value, "bookmark_preview");
  const actions = record(
    candidate.candidate_action_counts,
    "bookmark_preview.candidate_action_counts",
  );
  return {
    jobId: text(candidate.job_id, "bookmark_preview.job_id"),
    runId: text(candidate.run_id, "bookmark_preview.run_id"),
    jobVersion: version(candidate.job_version, "bookmark_preview.job_version"),
    folderCount: count(candidate.folder_count, "bookmark_preview.folder_count"),
    occurrenceCount: count(candidate.occurrence_count, "bookmark_preview.occurrence_count"),
    candidateCount: count(candidate.candidate_count, "bookmark_preview.candidate_count"),
    duplicateOccurrenceCount: count(
      candidate.duplicate_occurrence_count,
      "bookmark_preview.duplicate_occurrence_count",
    ),
    sensitiveCandidateCount: count(
      candidate.sensitive_candidate_count,
      "bookmark_preview.sensitive_candidate_count",
    ),
    actions: {
      create: count(actions.create, "bookmark_preview.actions.create"),
      skipExisting: count(actions.skip_existing, "bookmark_preview.actions.skip_existing"),
      mergeMissingMetadata: count(
        actions.merge_missing_metadata,
        "bookmark_preview.actions.merge_missing_metadata",
      ),
      reject: count(actions.reject, "bookmark_preview.actions.reject"),
      needsReview: count(actions.needs_review, "bookmark_preview.actions.needs_review"),
    },
  };
}

export function normalizeBookmarkImportResult(value: unknown): BookmarkImportResult {
  const candidate = record(value, "bookmark_result");
  return {
    jobId: text(candidate.job_id, "bookmark_result.job_id"),
    state: text(candidate.state, "bookmark_result.state"),
    jobVersion: version(candidate.job_version, "bookmark_result.job_version"),
    totalCandidates: count(candidate.total_candidates, "bookmark_result.total_candidates"),
    created: count(candidate.created, "bookmark_result.created"),
    skippedExisting: count(candidate.skipped_existing, "bookmark_result.skipped_existing"),
    skippedNeedsReview: count(
      candidate.skipped_needs_review,
      "bookmark_result.skipped_needs_review",
    ),
    failed: count(candidate.failed, "bookmark_result.failed"),
  };
}

const STATE_LABELS: Record<string, string> = {
  receiving: "接收中",
  queued_parse: "排队解析",
  parsing: "正在解析",
  parse_preview_ready: "解析完成，待确认",
  queued_classification: "排队分类",
  classifying: "正在分类",
  final_preview_ready: "分类完成，待确认",
  committing: "正在写入",
  completed: "已完成",
  completed_with_errors: "完成，但有失败项",
  cancel_requested: "正在取消",
  cancelled: "已取消",
  failed: "失败",
  expired: "已过期",
};

export function bookmarkImportStateLabel(state: string): string {
  return STATE_LABELS[state] ?? state;
}

const FAILURE_LABELS: Record<string, string> = {
  invalid_bookmark_file: "文件不是可识别的书签导出，请确认导出格式",
  processing_limit_exceeded: "文件超出了处理上限",
  classification_budget_exhausted: "分类额度已用尽",
  internal_error: "服务端处理失败，请重试",
};

export function bookmarkFailureLabel(code: string | null): string | null {
  if (!code) return null;
  return FAILURE_LABELS[code] ?? "导入失败，请重试";
}

export function bookmarkErrorMessage(status: number, payload: unknown): string {
  if (typeof payload === "object" && payload !== null && !Array.isArray(payload)) {
    const candidate = payload as JsonRecord;
    const detail = candidate.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (typeof detail === "object" && detail !== null && !Array.isArray(detail)) {
      const message = (detail as JsonRecord).message;
      if (typeof message === "string" && message.trim()) return message.trim();
    }
  }
  if (status === 401) return "登录状态已失效，请重新登录";
  if (status === 404) return "导入任务不存在";
  if (status === 409) return "导入任务已被更新，请刷新后重试";
  if (status === 413) return "文件太大，超出了上传上限";
  if (status === 415) return "只支持 HTML 格式的书签导出文件";
  if (status === 422) return "文件内容不符合要求";
  if (status === 429) return "上传过于频繁，请稍后重试";
  return "书签导入服务暂时不可用，请稍后重试";
}
