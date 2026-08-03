import { createContractGuards } from "./contract-guards.ts";

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

export const BOOKMARK_SIMILARITY_DECISIONS = [
  "merge_to_homepage",
  "keep_originals",
] as const;
export type BookmarkSimilarityDecision = (typeof BOOKMARK_SIMILARITY_DECISIONS)[number];

export const BOOKMARK_SIMILARITY_DECISION_FILTERS = [
  "unresolved",
  ...BOOKMARK_SIMILARITY_DECISIONS,
] as const;
export type BookmarkSimilarityDecisionFilter =
  (typeof BOOKMARK_SIMILARITY_DECISION_FILTERS)[number];

const BOOKMARK_SIMILARITY_CONFIDENCES = ["high", "medium", "low"] as const;
export type BookmarkSimilarityConfidence =
  (typeof BOOKMARK_SIMILARITY_CONFIDENCES)[number];

const BOOKMARK_SIMILARITY_CANONICAL_SOURCES = [
  "imported_homepage",
  "derived_origin_root",
  "existing_library",
] as const;
export type BookmarkSimilarityCanonicalSource =
  (typeof BOOKMARK_SIMILARITY_CANONICAL_SOURCES)[number];

export type BookmarkSimilarityDecisionCounts = {
  unresolved: number;
  mergeToHomepage: number;
  keepOriginals: number;
};

export type BookmarkPreviewSummary = {
  jobId: string;
  runId: string;
  jobVersion: number;
  decisionVersion: number;
  folderCount: number;
  occurrenceCount: number;
  candidateCount: number;
  duplicateOccurrenceCount: number;
  sensitiveCandidateCount: number;
  similarityClusterCount: number;
  similarityCandidateCount: number;
  similarityDecisions: BookmarkSimilarityDecisionCounts;
  selectedMergeReductionCount: number;
  projectedCreateCount: number;
  actions: {
    create: number;
    skipExisting: number;
    mergeMissingMetadata: number;
    reject: number;
    needsReview: number;
  };
};

export type BookmarkSimilarityCanonical = {
  candidateId: string | null;
  url: string;
  title: string;
  source: BookmarkSimilarityCanonicalSource;
};

export type BookmarkSimilarityMember = {
  candidateId: string;
  title: string;
  displayUrl: string;
  occurrenceCount: number;
  firstSourceSequence: number;
  isCanonical: boolean;
};

export type BookmarkSimilarityCluster = {
  id: string;
  displayHost: string;
  confidence: BookmarkSimilarityConfidence;
  reasonCodes: string[];
  candidateCount: number;
  occurrenceCount: number;
  firstSourceSequence: number;
  decision: BookmarkSimilarityDecision | null;
  canonical: BookmarkSimilarityCanonical;
  sampleMembers: BookmarkSimilarityMember[];
  hasMoreMembers: boolean;
};

export type BookmarkSimilarityClusterPage = {
  items: BookmarkSimilarityCluster[];
  nextCursor: string | null;
  page: number;
  pageSize: number;
  totalCount: number;
  totalPages: number;
  decisionVersion: number;
};

export type BookmarkSimilarityMemberPage = {
  items: BookmarkSimilarityMember[];
  nextCursor: string | null;
  decisionVersion: number;
};

export type BookmarkSimilarityDecisionResult = {
  jobId: string;
  runId: string;
  jobVersion: number;
  decisionVersion: number;
  similarityDecisions: BookmarkSimilarityDecisionCounts;
  selectedMergeReductionCount: number;
  projectedCreateCount: number;
};

export type BookmarkImportResult = {
  jobId: string;
  state: string;
  jobVersion: number;
  totalCandidates: number;
  created: number;
  skippedExisting: number;
  skippedNeedsReview: number;
  mergedCandidates: number;
  failed: number;
};

type JsonRecord = Record<string, unknown>;

export class BookmarkContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BookmarkContractError";
  }
}

// 校验原语与其他契约模块完全一致，统一放在 contract-guards；
// 这里只绑定本模块自己的错误类型，便于调用方按类型区分来源。
const {
  record,
  text,
  identifier,
  boolean,
  count,
  version,
  absoluteWebUrl,
  literal,
} = createContractGuards((message) => new BookmarkContractError(message));

function nullableCursor(value: unknown, path: string): string | null {
  if (value === null) return null;
  return text(value, path);
}

function normalizeSimilarityDecisionCountsAt(
  value: unknown,
  path: string,
): BookmarkSimilarityDecisionCounts {
  const candidate = record(value, path);
  return {
    unresolved: count(candidate.unresolved, `${path}.unresolved`),
    mergeToHomepage: count(candidate.merge_to_homepage, `${path}.merge_to_homepage`),
    keepOriginals: count(candidate.keep_originals, `${path}.keep_originals`),
  };
}

function normalizeSimilarityMemberAt(
  value: unknown,
  path: string,
): BookmarkSimilarityMember {
  const candidate = record(value, path);
  return {
    candidateId: identifier(candidate.candidate_id, `${path}.candidate_id`),
    title: text(candidate.title, `${path}.title`),
    displayUrl: absoluteWebUrl(candidate.display_url, `${path}.display_url`),
    occurrenceCount: count(candidate.occurrence_count, `${path}.occurrence_count`),
    firstSourceSequence: count(
      candidate.first_source_sequence,
      `${path}.first_source_sequence`,
    ),
    isCanonical: boolean(candidate.is_canonical, `${path}.is_canonical`),
  };
}

function normalizeSimilarityClusterAt(
  value: unknown,
  path: string,
): BookmarkSimilarityCluster {
  const candidate = record(value, path);
  const canonical = record(candidate.canonical, `${path}.canonical`);
  if (!Array.isArray(candidate.reason_codes)) {
    throw new BookmarkContractError(`${path}.reason_codes 必须是数组`);
  }
  if (!Array.isArray(candidate.sample_members)) {
    throw new BookmarkContractError(`${path}.sample_members 必须是数组`);
  }
  return {
    id: identifier(candidate.id, `${path}.id`),
    displayHost: text(candidate.display_host, `${path}.display_host`),
    confidence: literal(
      candidate.confidence,
      `${path}.confidence`,
      BOOKMARK_SIMILARITY_CONFIDENCES,
    ),
    reasonCodes: candidate.reason_codes.map((reason, index) =>
      text(reason, `${path}.reason_codes[${index}]`),
    ),
    candidateCount: count(candidate.candidate_count, `${path}.candidate_count`),
    occurrenceCount: count(candidate.occurrence_count, `${path}.occurrence_count`),
    firstSourceSequence: count(
      candidate.first_source_sequence,
      `${path}.first_source_sequence`,
    ),
    decision:
      candidate.decision === null
        ? null
        : literal(
            candidate.decision,
            `${path}.decision`,
            BOOKMARK_SIMILARITY_DECISIONS,
          ),
    canonical: {
      candidateId:
        canonical.candidate_id === null
          ? null
          : identifier(canonical.candidate_id, `${path}.canonical.candidate_id`),
      url: absoluteWebUrl(canonical.url, `${path}.canonical.url`),
      title: text(canonical.title, `${path}.canonical.title`),
      source: literal(
        canonical.source,
        `${path}.canonical.source`,
        BOOKMARK_SIMILARITY_CANONICAL_SOURCES,
      ),
    },
    sampleMembers: candidate.sample_members.map((member, index) =>
      normalizeSimilarityMemberAt(member, `${path}.sample_members[${index}]`),
    ),
    hasMoreMembers: boolean(candidate.has_more_members, `${path}.has_more_members`),
  };
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
    decisionVersion: version(
      candidate.decision_version,
      "bookmark_preview.decision_version",
    ),
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
    similarityClusterCount: count(
      candidate.similarity_cluster_count,
      "bookmark_preview.similarity_cluster_count",
    ),
    similarityCandidateCount: count(
      candidate.similarity_candidate_count,
      "bookmark_preview.similarity_candidate_count",
    ),
    similarityDecisions: normalizeSimilarityDecisionCountsAt(
      candidate.similarity_decision_counts,
      "bookmark_preview.similarity_decision_counts",
    ),
    selectedMergeReductionCount: count(
      candidate.selected_merge_reduction_count,
      "bookmark_preview.selected_merge_reduction_count",
    ),
    projectedCreateCount: count(
      candidate.projected_create_count,
      "bookmark_preview.projected_create_count",
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

export function normalizeBookmarkSimilarityClusterPage(
  value: unknown,
): BookmarkSimilarityClusterPage {
  const candidate = record(value, "bookmark_similarity_clusters");
  if (!Array.isArray(candidate.items)) {
    throw new BookmarkContractError("bookmark_similarity_clusters.items 必须是数组");
  }
  const items = candidate.items.map((item, index) =>
    normalizeSimilarityClusterAt(item, `bookmark_similarity_clusters.items[${index}]`),
  );
  const nextCursor = nullableCursor(
    candidate.next_cursor,
    "bookmark_similarity_clusters.next_cursor",
  );
  const page = version(candidate.page, "bookmark_similarity_clusters.page");
  const pageSize = version(candidate.page_size, "bookmark_similarity_clusters.page_size");
  const totalCount = count(candidate.total_count, "bookmark_similarity_clusters.total_count");
  const totalPages = count(candidate.total_pages, "bookmark_similarity_clusters.total_pages");
  const expectedTotalPages = totalCount === 0 ? 0 : Math.ceil(totalCount / pageSize);
  if (pageSize > 50 || totalPages !== expectedTotalPages || page > Math.max(totalPages, 1)) {
    throw new BookmarkContractError("bookmark_similarity_clusters 的页码元数据不一致");
  }
  if (
    items.length > pageSize
    || items.length > totalCount
    || (totalCount === 0 && nextCursor !== null)
    || (page === totalPages && nextCursor !== null)
  ) {
    throw new BookmarkContractError("bookmark_similarity_clusters 的分页内容超出范围");
  }
  return {
    items,
    nextCursor,
    page,
    pageSize,
    totalCount,
    totalPages,
    decisionVersion: version(
      candidate.decision_version,
      "bookmark_similarity_clusters.decision_version",
    ),
  };
}

export function normalizeBookmarkSimilarityMemberPage(
  value: unknown,
): BookmarkSimilarityMemberPage {
  const candidate = record(value, "bookmark_similarity_members");
  if (!Array.isArray(candidate.items)) {
    throw new BookmarkContractError("bookmark_similarity_members.items 必须是数组");
  }
  return {
    items: candidate.items.map((item, index) =>
      normalizeSimilarityMemberAt(item, `bookmark_similarity_members.items[${index}]`),
    ),
    nextCursor: nullableCursor(
      candidate.next_cursor,
      "bookmark_similarity_members.next_cursor",
    ),
    decisionVersion: version(
      candidate.decision_version,
      "bookmark_similarity_members.decision_version",
    ),
  };
}

export function normalizeBookmarkSimilarityDecisionResult(
  value: unknown,
): BookmarkSimilarityDecisionResult {
  const candidate = record(value, "bookmark_similarity_decision");
  return {
    jobId: text(candidate.job_id, "bookmark_similarity_decision.job_id"),
    runId: text(candidate.run_id, "bookmark_similarity_decision.run_id"),
    jobVersion: version(candidate.job_version, "bookmark_similarity_decision.job_version"),
    decisionVersion: version(
      candidate.decision_version,
      "bookmark_similarity_decision.decision_version",
    ),
    similarityDecisions: normalizeSimilarityDecisionCountsAt(
      candidate.similarity_decision_counts,
      "bookmark_similarity_decision.similarity_decision_counts",
    ),
    selectedMergeReductionCount: count(
      candidate.selected_merge_reduction_count,
      "bookmark_similarity_decision.selected_merge_reduction_count",
    ),
    projectedCreateCount: count(
      candidate.projected_create_count,
      "bookmark_similarity_decision.projected_create_count",
    ),
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
    mergedCandidates: count(candidate.merged_candidates, "bookmark_result.merged_candidates"),
    failed: count(candidate.failed, "bookmark_result.failed"),
  };
}

const SIMILARITY_REASON_LABELS: Record<string, string> = {
  same_site_authority: "同一站点",
  www_alias: "www 地址变体",
  http_https_variants: "HTTP/HTTPS 地址变体",
  shared_path_variants: "相同路径的地址变体",
  existing_library_homepage: "网址库已有推荐主页",
  homepage_and_subpages: "主页与子页面",
  common_path_ancestor: "共同页面入口",
  derived_origin_root: "可归并到站点首页",
};

export function bookmarkSimilarityReasonLabel(code: string): string {
  return SIMILARITY_REASON_LABELS[code] ?? "同站点相似页面";
}

const CANONICAL_SOURCE_LABELS: Record<BookmarkSimilarityCanonicalSource, string> = {
  imported_homepage: "书签中的推荐主页",
  derived_origin_root: "根据站点地址推导的推荐主页",
  existing_library: "网址库中已有的推荐主页",
};

export function bookmarkCanonicalSourceLabel(
  source: BookmarkSimilarityCanonicalSource,
): string {
  return CANONICAL_SOURCE_LABELS[source];
}

export function bookmarkSimilarityConfidenceLabel(
  confidence: BookmarkSimilarityConfidence,
): string {
  if (confidence === "high") return "高置信度";
  if (confidence === "medium") return "中置信度";
  return "低置信度";
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
