import { createContractGuards } from "./contract-guards.ts";

// "relevance" 只在有搜索词时可用：按相关度排「什么都没搜」是没有意义的，
// 后端会 422。配了 embedding Provider 且建过索引时，它是唯一会带语义召回的排序。
export type LibrarySort = "created" | "updated" | "name" | "custom" | "relevance";
export type LibraryDirection = "asc" | "desc";

const LIBRARY_SITE_SOURCES = ["manual", "agent", "browser_import", "backup"] as const;
const LIBRARY_SITE_CREATE_SOURCES = ["manual", "agent"] as const;
const LIBRARY_ANALYSIS_STATUSES = ["not_analyzed", "pending", "complete", "failed", "limited"] as const;
const LIBRARY_ANALYSIS_PHASES = [
  "fetching_page",
  "preparing_evidence",
  "waiting_model",
  "calling_model",
  "saving_result",
] as const;
const LIBRARY_SITE_ANALYSIS_OUTCOMES = ["complete", "limited", "failed"] as const;
const METADATA_BACKFILL_STATUSES = [
  "queued",
  "running",
  "completed",
  "completed_with_errors",
  "failed",
] as const;
const METADATA_BACKFILL_MODES = ["metadata", "full"] as const;
const METADATA_BACKFILL_STOP_REASONS = [
  "provider_rate_limited",
  "provider_temporary_failure",
  "provider_unavailable",
  "internal_error",
] as const;
const SITE_SIMILARITY_RUN_STATUSES = ["ready", "applied", "superseded"] as const;
const SITE_SIMILARITY_GROUP_KINDS = ["duplicate", "same_site"] as const;
const SITE_SIMILARITY_GROUP_FILTERS = ["duplicate", "same_site", "all"] as const;

export type LibrarySiteSource = (typeof LIBRARY_SITE_SOURCES)[number];
export type LibrarySiteCreateSource = (typeof LIBRARY_SITE_CREATE_SOURCES)[number];
export type LibraryAnalysisStatus = (typeof LIBRARY_ANALYSIS_STATUSES)[number];
export type LibraryAnalysisPhase = (typeof LIBRARY_ANALYSIS_PHASES)[number];
export type LibrarySiteAnalysisOutcome = (typeof LIBRARY_SITE_ANALYSIS_OUTCOMES)[number];
export type MetadataBackfillStatus = (typeof METADATA_BACKFILL_STATUSES)[number];
export type MetadataBackfillMode = (typeof METADATA_BACKFILL_MODES)[number];
export type MetadataBackfillStopReason = (typeof METADATA_BACKFILL_STOP_REASONS)[number];
export type SiteSimilarityRunStatus = (typeof SITE_SIMILARITY_RUN_STATUSES)[number];
export type SiteSimilarityGroupKind = (typeof SITE_SIMILARITY_GROUP_KINDS)[number];

export const METADATA_BACKFILL_LIMITS = {
  metadata: { defaultLimit: 200, maxLimit: 500 },
  full: { defaultLimit: 50, maxLimit: 100 },
} as const satisfies Record<
  MetadataBackfillMode,
  Readonly<{ defaultLimit: number; maxLimit: number }>
>;

export function isMetadataBackfillTerminalStatus(status: MetadataBackfillStatus): boolean {
  return status !== "queued" && status !== "running";
}

export const MAX_LIBRARY_SITE_NAME_LENGTH = 160;
export const MIN_LIBRARY_SITE_SUMMARY_LENGTH = 20;
export const MAX_LIBRARY_SITE_SUMMARY_LENGTH = 50;
export const MAX_LIBRARY_CATEGORY_NAME_LENGTH = 80;
export const MAX_LIBRARY_TAG_NAME_LENGTH = 40;
export const MAX_LIBRARY_BULK_DELETE_SITES = 100;

/** Match the backend taxonomy identity rule before comparing or submitting tags. */
export function normalizeLibraryTagName(name: string): string {
  return name.normalize("NFKC").trim().replace(/\s+/gu, " ");
}

/**
 * JavaScript has no native `casefold`; lower-casing plus the two common
 * case-fold-only mappings keeps browser conflict recovery aligned with Python.
 */
export function libraryTagNameKey(name: string): string {
  return normalizeLibraryTagName(name)
    .toLowerCase()
    .replaceAll("ß", "ss")
    .replace(/\u03c2/gu, "\u03c3");
}

export type LibraryCategoryRef = {
  id: string;
  name: string;
  isDefault: boolean;
  icon: string;
};

export type LibraryCategory = LibraryCategoryRef & {
  siteCount: number;
};

export type LibraryTagRef = {
  id: string;
  name: string;
};

export type LibraryTag = LibraryTagRef & {
  siteCount: number;
};

export type LibrarySite = {
  id: string;
  name: string;
  originalUrl: string;
  identityUrl: string;
  summary: string | null;
  description: string | null;
  faviconUrl: string | null;
  previewUrl: string | null;
  category: LibraryCategoryRef;
  tags: LibraryTagRef[];
  pinned: boolean;
  source: LibrarySiteSource;
  analysisStatus: LibraryAnalysisStatus;
  analysisPhase: LibraryAnalysisPhase | null;
  version: number;
  createdAt: string;
  updatedAt: string;
};

export type LibrarySiteSelectionItem = Pick<
  LibrarySite,
  "id" | "name" | "originalUrl" | "faviconUrl" | "version"
>;

export type LibrarySitePage = {
  items: LibrarySite[];
  nextCursor: string | null;
  aggregate: {
    matchedCount: number;
    pinnedCount: number;
  };
};

export type LibraryAnalysisBackfill = {
  queuedCount: number;
  activeCount: number;
  remainingCount: number;
};

export type LibrarySiteAnalysisResult = {
  site: LibrarySite;
  outcome: LibrarySiteAnalysisOutcome;
  message: string;
  llmApplied: boolean;
};

export type MetadataBackfillRequest = {
  mode: MetadataBackfillMode;
  limit: number;
};

export type MetadataBackfillPlan = {
  mode: MetadataBackfillMode;
  requestedLimit: number;
  eligibleCount: number;
  selectedCount: number;
  llmCount: number;
  maxLimit: number;
};

/**
 * A durable, account-scoped metadata backfill run.  `completedCount` is the
 * number that has reached a terminal outcome; its result counters explain the
 * outcome split.  Together with queued/running, the counters partition the
 * immutable run total, so clients can show exact progress without inferring it
 * from a momentary worker queue.
 */
export type MetadataBackfillProgress = {
  runId: string;
  mode: MetadataBackfillMode;
  status: MetadataBackfillStatus;
  stoppedEarly: boolean;
  stopReason: MetadataBackfillStopReason | null;
  providerRetryAt: string | null;
  totalCount: number;
  queuedCount: number;
  runningCount: number;
  completedCount: number;
  completeCount: number;
  failedCount: number;
  limitedCount: number;
  skippedCount: number;
  /** POST reports whether it attached to an already running account job. */
  reused?: boolean;
};

export type SiteSimilarityScan = {
  runId: string;
  status: SiteSimilarityRunStatus;
  rulesetVersion: string;
  sourceSiteCount: number;
  groupCount: number;
  duplicateGroupCount: number;
  sameSiteGroupCount: number;
  candidateSiteCount: number;
  selectedGroupCount: number;
  selectedDeleteCount: number;
  version: number;
  decisionVersion: number;
  createdAt: string;
  appliedAt: string | null;
};

export type SiteSimilarityMember = LibrarySite & {
  isRecommended: boolean;
};

export type SiteSimilarityGroup = {
  id: string;
  kind: SiteSimilarityGroupKind;
  siteKey: string;
  displayHost: string;
  memberCount: number;
  recommendedSiteId: string;
  /** An empty list means every member remains in the library. */
  keepSiteIds: string[];
  members: SiteSimilarityMember[];
};

export type SiteSimilarityGroupPage = {
  items: SiteSimilarityGroup[];
  nextCursor: string | null;
  page: number;
  pageSize: number;
  totalCount: number;
  totalPages: number;
  decisionVersion: number;
};

export type SiteSimilarityDecisionResult = {
  groupId: string;
  keepSiteIds: string[];
  decisionVersion: number;
  selectedGroupCount: number;
  selectedDeleteCount: number;
};

export type SiteSimilarityRecommendedDecisionResult = {
  kind: SiteSimilarityGroupKind | "all";
  matchedGroupCount: number;
  updatedGroupCount: number;
  decisionVersion: number;
  selectedGroupCount: number;
  selectedDeleteCount: number;
};

export type SiteSimilarityApplyResult = {
  runId: string;
  status: "applied";
  decisionVersion: number;
  mergedGroupCount: number;
  deletedSiteCount: number;
  keptSiteCount: number;
  deletedSiteIds: string[];
  keptSiteIds: string[];
  appliedAt: string;
};

export type LibraryCategoryDeletePreview = {
  category: LibraryCategory;
  affectedSiteCount: number;
  replacementCategory: LibraryCategory | null;
};

export type LibrarySiteQuery = {
  q?: string;
  categoryId?: string;
  tagId?: string;
  pinned?: boolean;
  sort?: LibrarySort;
  direction?: LibraryDirection;
  cursor?: string;
  limit?: number;
};

export type LibrarySiteCreateInput = {
  name: string;
  url: string;
  summary?: string | null;
  description?: string | null;
  faviconUrl?: string | null;
  categoryId?: string;
  tagIds?: string[];
  pinned?: boolean;
  source?: LibrarySiteCreateSource;
};

export type LibrarySiteUpdateInput = Omit<
  Partial<LibrarySiteCreateInput>,
  "categoryId" | "source"
> & {
  categoryId?: string | null;
  expectedVersion: number;
};

export type LibraryBulkDeleteItem = {
  siteId: string;
  expectedVersion: number;
};

export type LibraryBulkDeleteResult = {
  deletedSiteIds: string[];
};

export type LibraryErrorDetails = {
  code?: string;
  message: string;
};

type JsonRecord = Record<string, unknown>;

export class LibraryContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LibraryContractError";
  }
}

export function librarySiteCardSummary(
  site: Readonly<{ summary?: string | null; description?: string | null }>,
): string {
  return site.summary?.trim() || site.description?.trim() || "";
}

// 校验原语与其他契约模块完全一致，统一放在 contract-guards；
// 这里只绑定本模块自己的错误类型，便于调用方按类型区分来源。
const {
  record,
  text,
  boundedText,
  nullableText,
  identifier,
  boolean,
  count,
  version,
  absoluteWebUrl,
  nullableWebUrl,
  isoDate,
  listPayload,
} = createContractGuards((message) => new LibraryContractError(message));

function literal<const Values extends readonly string[]>(
  value: unknown,
  path: string,
  values: Values,
): Values[number] {
  if (typeof value !== "string" || !values.some((candidate) => candidate === value)) {
    throw new LibraryContractError(`${path} 不是受支持的值`);
  }
  return value as Values[number];
}

function siteSummary(value: unknown): string | null {
  const summary = nullableText(value, "site.summary");
  if (summary !== null && Array.from(summary).length > MAX_LIBRARY_SITE_SUMMARY_LENGTH) {
    throw new LibraryContractError(
      `site.summary 不能超过 ${MAX_LIBRARY_SITE_SUMMARY_LENGTH} 个字符`,
    );
  }
  return summary;
}

function assertedSiteSummary(value: unknown): string | null {
  const summary = siteSummary(value);
  if (summary !== null && Array.from(summary).length < MIN_LIBRARY_SITE_SUMMARY_LENGTH) {
    throw new LibraryContractError(
      `site.summary 不能少于 ${MIN_LIBRARY_SITE_SUMMARY_LENGTH} 个字符`,
    );
  }
  return summary;
}

function normalizeCategoryRefAt(value: unknown, path: string): LibraryCategoryRef {
  const candidate = record(value, path);
  return {
    id: identifier(candidate.id, `${path}.id`),
    name: text(candidate.name, `${path}.name`),
    isDefault: boolean(candidate.is_default, `${path}.is_default`),
    icon: text(candidate.icon, `${path}.icon`),
  };
}

function normalizeTagRefAt(value: unknown, path: string): LibraryTagRef {
  const candidate = record(value, path);
  return {
    id: identifier(candidate.id, `${path}.id`),
    name: text(candidate.name, `${path}.name`),
  };
}

export function normalizeLibraryCategory(value: unknown): LibraryCategory {
  const candidate = record(value, "category");
  return {
    ...normalizeCategoryRefAt(candidate, "category"),
    siteCount: count(candidate.site_count, "category.site_count"),
  };
}

export function normalizeLibraryTag(value: unknown): LibraryTag {
  const candidate = record(value, "tag");
  return {
    ...normalizeTagRefAt(candidate, "tag"),
    siteCount: count(candidate.site_count, "tag.site_count"),
  };
}

export function normalizeLibrarySite(value: unknown): LibrarySite {
  const candidate = record(value, "site");
  if (!Array.isArray(candidate.tags)) throw new LibraryContractError("site.tags 必须是数组");

  return {
    id: identifier(candidate.id, "site.id"),
    name: text(candidate.name, "site.name"),
    originalUrl: absoluteWebUrl(candidate.original_url, "site.original_url"),
    identityUrl: absoluteWebUrl(candidate.identity_url, "site.identity_url"),
    summary: siteSummary(candidate.summary),
    description: nullableText(candidate.description, "site.description"),
    faviconUrl: nullableWebUrl(candidate.favicon_url, "site.favicon_url"),
    previewUrl: nullableWebUrl(candidate.preview_url, "site.preview_url"),
    category: normalizeCategoryRefAt(candidate.category, "site.category"),
    tags: candidate.tags.map((tag, index) => normalizeTagRefAt(tag, `site.tags[${index}]`)),
    pinned: boolean(candidate.pinned, "site.pinned"),
    source: literal(candidate.source, "site.source", LIBRARY_SITE_SOURCES),
    analysisStatus: literal(candidate.analysis_status, "site.analysis_status", LIBRARY_ANALYSIS_STATUSES),
    analysisPhase: candidate.analysis_phase === null
      ? null
      : literal(candidate.analysis_phase, "site.analysis_phase", LIBRARY_ANALYSIS_PHASES),
    version: version(candidate.version, "site.version"),
    createdAt: isoDate(candidate.created_at, "site.created_at"),
    updatedAt: isoDate(candidate.updated_at, "site.updated_at"),
  };
}

export function normalizeLibrarySiteAnalysis(value: unknown): LibrarySiteAnalysisResult {
  const candidate = record(value, "site_analysis");
  return {
    site: normalizeLibrarySite(candidate.site),
    outcome: literal(
      candidate.outcome,
      "site_analysis.outcome",
      LIBRARY_SITE_ANALYSIS_OUTCOMES,
    ),
    message: text(candidate.message, "site_analysis.message"),
    llmApplied: boolean(candidate.llm_applied, "site_analysis.llm_applied"),
  };
}

export function normalizeLibrarySiteSelection(value: unknown): LibrarySiteSelectionItem[] {
  const candidate = record(value, "site_selection");
  if (!Array.isArray(candidate.items)) {
    throw new LibraryContractError("site_selection.items 必须是数组");
  }
  const items = candidate.items.map((item, index) => {
    const site = record(item, `site_selection.items[${index}]`);
    return {
      id: identifier(site.id, `site_selection.items[${index}].id`),
      name: text(site.name, `site_selection.items[${index}].name`),
      originalUrl: absoluteWebUrl(
        site.original_url,
        `site_selection.items[${index}].original_url`,
      ),
      faviconUrl: nullableWebUrl(
        site.favicon_url,
        `site_selection.items[${index}].favicon_url`,
      ),
      version: version(site.version, `site_selection.items[${index}].version`),
    };
  });
  if (new Set(items.map((item) => item.id)).size !== items.length) {
    throw new LibraryContractError("site_selection.items 不能包含重复网站");
  }
  return items;
}

export function normalizeLibraryAnalysisBackfill(value: unknown): LibraryAnalysisBackfill {
  const candidate = record(value, "analysis_backfill");
  return {
    queuedCount: count(candidate.queued_count, "analysis_backfill.queued_count"),
    activeCount: count(candidate.active_count, "analysis_backfill.active_count"),
    remainingCount: count(candidate.remaining_count, "analysis_backfill.remaining_count"),
  };
}

export function normalizeMetadataBackfillProgress(value: unknown): MetadataBackfillProgress {
  const candidate = record(value, "metadata_backfill");
  const mode = literal(candidate.mode, "metadata_backfill.mode", METADATA_BACKFILL_MODES);
  const status = literal(candidate.status, "metadata_backfill.status", METADATA_BACKFILL_STATUSES);
  const stopReason = candidate.stop_reason === null
    ? null
    : literal(
      candidate.stop_reason,
      "metadata_backfill.stop_reason",
      METADATA_BACKFILL_STOP_REASONS,
    );
  const progress: MetadataBackfillProgress = {
    runId: identifier(candidate.id, "metadata_backfill.id"),
    mode,
    status,
    stoppedEarly: boolean(candidate.stopped_early, "metadata_backfill.stopped_early"),
    stopReason,
    providerRetryAt: candidate.provider_retry_at === null
      ? null
      : isoDate(candidate.provider_retry_at, "metadata_backfill.provider_retry_at"),
    totalCount: count(candidate.total_count, "metadata_backfill.total_count"),
    queuedCount: count(candidate.queued_count, "metadata_backfill.queued_count"),
    runningCount: count(candidate.running_count, "metadata_backfill.running_count"),
    completedCount: count(candidate.completed_count, "metadata_backfill.completed_count"),
    completeCount: count(candidate.complete_count, "metadata_backfill.complete_count"),
    failedCount: count(candidate.failed_count, "metadata_backfill.failed_count"),
    limitedCount: count(candidate.limited_count, "metadata_backfill.limited_count"),
    skippedCount: count(candidate.skipped_count, "metadata_backfill.skipped_count"),
    ...(candidate.reused === undefined
      ? {}
      : { reused: boolean(candidate.reused, "metadata_backfill.reused") }),
  };
  const activeAndTerminalCount = progress.queuedCount
    + progress.runningCount
    + progress.completedCount;
  if (activeAndTerminalCount !== progress.totalCount) {
    throw new LibraryContractError("metadata_backfill 的状态计数必须等于 total_count");
  }
  const outcomeCount = progress.completeCount
    + progress.failedCount
    + progress.limitedCount
    + progress.skippedCount;
  if (outcomeCount !== progress.completedCount) {
    throw new LibraryContractError("metadata_backfill 的终态结果计数必须等于 completed_count");
  }
  if (
    isMetadataBackfillTerminalStatus(progress.status) &&
    (progress.queuedCount > 0 || progress.runningCount > 0)
  ) {
    throw new LibraryContractError("已完成的 metadata_backfill 不能保留待处理网站");
  }
  return progress;
}

function identifierListAt(value: unknown, path: string): string[] {
  if (!Array.isArray(value)) {
    throw new LibraryContractError(`${path} 必须是数组`);
  }
  const identifiers = value.map((item, index) => identifier(item, `${path}[${index}]`));
  if (new Set(identifiers).size !== identifiers.length) {
    throw new LibraryContractError(`${path} 不能包含重复网站`);
  }
  return identifiers;
}

function nullableCursorAt(value: unknown, path: string): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || !value.trim()) {
    throw new LibraryContractError(`${path} 必须是非空字符串或 null`);
  }
  return value.trim();
}

export function normalizeSiteSimilarityScan(value: unknown): SiteSimilarityScan {
  const candidate = record(value, "site_similarity_scan");
  const status = literal(
    candidate.status,
    "site_similarity_scan.status",
    SITE_SIMILARITY_RUN_STATUSES,
  );
  const duplicateGroupCount = count(
    candidate.duplicate_group_count,
    "site_similarity_scan.duplicate_group_count",
  );
  const sameSiteGroupCount = count(
    candidate.same_site_group_count,
    "site_similarity_scan.same_site_group_count",
  );
  const groupCount = count(candidate.group_count, "site_similarity_scan.group_count");
  if (groupCount !== duplicateGroupCount + sameSiteGroupCount) {
    throw new LibraryContractError("site_similarity_scan 的分组计数不一致");
  }
  const selectedGroupCount = count(
    candidate.selected_group_count,
    "site_similarity_scan.selected_group_count",
  );
  const candidateSiteCount = count(
    candidate.candidate_site_count,
    "site_similarity_scan.candidate_site_count",
  );
  const selectedDeleteCount = count(
    candidate.selected_delete_count,
    "site_similarity_scan.selected_delete_count",
  );
  if (selectedGroupCount > groupCount || selectedDeleteCount > candidateSiteCount) {
    throw new LibraryContractError("site_similarity_scan 的清理选择计数超出扫描范围");
  }
  const appliedAt = candidate.applied_at === null
    ? null
    : isoDate(candidate.applied_at, "site_similarity_scan.applied_at");
  if ((status === "applied") !== (appliedAt !== null)) {
    throw new LibraryContractError("site_similarity_scan 的应用状态与时间不一致");
  }
  return {
    runId: identifier(candidate.id, "site_similarity_scan.id"),
    status,
    rulesetVersion: text(candidate.ruleset_version, "site_similarity_scan.ruleset_version"),
    sourceSiteCount: count(
      candidate.source_site_count,
      "site_similarity_scan.source_site_count",
    ),
    groupCount,
    duplicateGroupCount,
    sameSiteGroupCount,
    candidateSiteCount,
    selectedGroupCount,
    selectedDeleteCount,
    version: version(candidate.version, "site_similarity_scan.version"),
    decisionVersion: version(
      candidate.decision_version,
      "site_similarity_scan.decision_version",
    ),
    createdAt: isoDate(candidate.created_at, "site_similarity_scan.created_at"),
    appliedAt,
  };
}

function normalizeSiteSimilarityMemberAt(
  value: unknown,
  path: string,
): SiteSimilarityMember {
  const candidate = record(value, path);
  return {
    ...normalizeLibrarySite(candidate),
    isRecommended: boolean(candidate.is_recommended, `${path}.is_recommended`),
  };
}

function normalizeSiteSimilarityGroupAt(value: unknown, path: string): SiteSimilarityGroup {
  const candidate = record(value, path);
  if (!Array.isArray(candidate.members)) {
    throw new LibraryContractError(`${path}.members 必须是数组`);
  }
  const members = candidate.members.map((member, index) =>
    normalizeSiteSimilarityMemberAt(member, `${path}.members[${index}]`));
  const memberCount = count(candidate.member_count, `${path}.member_count`);
  if (memberCount < 2 || members.length !== memberCount) {
    throw new LibraryContractError(`${path}.member_count 必须与完整成员列表一致且至少为 2`);
  }
  const memberIds = members.map((member) => member.id);
  if (new Set(memberIds).size !== memberIds.length) {
    throw new LibraryContractError(`${path}.members 不能包含重复网站`);
  }
  const recommendedSiteId = identifier(
    candidate.recommended_site_id,
    `${path}.recommended_site_id`,
  );
  const recommendedMembers = members.filter((member) => member.isRecommended);
  if (
    !memberIds.includes(recommendedSiteId)
    || recommendedMembers.length !== 1
    || recommendedMembers[0]?.id !== recommendedSiteId
  ) {
    throw new LibraryContractError(`${path}.recommended_site_id 与推荐成员不一致`);
  }
  const keepSiteIds = identifierListAt(candidate.keep_site_ids, `${path}.keep_site_ids`);
  if (keepSiteIds.some((siteId) => !memberIds.includes(siteId))) {
    throw new LibraryContractError(`${path}.keep_site_ids 包含当前分组之外的网站`);
  }
  if (keepSiteIds.length === memberCount) {
    throw new LibraryContractError(`${path}.keep_site_ids 全选时必须归一化为全部保留`);
  }
  return {
    id: identifier(candidate.id, `${path}.id`),
    kind: literal(candidate.kind, `${path}.kind`, SITE_SIMILARITY_GROUP_KINDS),
    siteKey: text(candidate.site_key, `${path}.site_key`),
    displayHost: text(candidate.display_host, `${path}.display_host`),
    memberCount,
    recommendedSiteId,
    keepSiteIds,
    members,
  };
}

export function normalizeSiteSimilarityGroupPage(value: unknown): SiteSimilarityGroupPage {
  const candidate = record(value, "site_similarity_groups");
  if (!Array.isArray(candidate.items)) {
    throw new LibraryContractError("site_similarity_groups.items 必须是数组");
  }
  const items = candidate.items.map((item, index) =>
    normalizeSiteSimilarityGroupAt(item, `site_similarity_groups.items[${index}]`));
  if (new Set(items.map((item) => item.id)).size !== items.length) {
    throw new LibraryContractError("site_similarity_groups.items 不能包含重复分组");
  }
  const nextCursor = nullableCursorAt(
    candidate.next_cursor,
    "site_similarity_groups.next_cursor",
  );
  const page = version(candidate.page, "site_similarity_groups.page");
  const pageSize = version(candidate.page_size, "site_similarity_groups.page_size");
  const totalCount = count(candidate.total_count, "site_similarity_groups.total_count");
  const totalPages = count(candidate.total_pages, "site_similarity_groups.total_pages");
  const expectedTotalPages = totalCount === 0 ? 0 : Math.ceil(totalCount / pageSize);
  if (pageSize > 24 || totalPages !== expectedTotalPages || page > Math.max(totalPages, 1)) {
    throw new LibraryContractError("site_similarity_groups 的页码元数据不一致");
  }
  if (
    items.length > pageSize
    || items.length > totalCount
    || (totalCount === 0 && nextCursor !== null)
    || (page === totalPages && nextCursor !== null)
  ) {
    throw new LibraryContractError("site_similarity_groups 的分页内容超出范围");
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
      "site_similarity_groups.decision_version",
    ),
  };
}

export function normalizeSiteSimilarityDecision(
  value: unknown,
): SiteSimilarityDecisionResult {
  const candidate = record(value, "site_similarity_decision");
  return {
    groupId: identifier(candidate.group_id, "site_similarity_decision.group_id"),
    keepSiteIds: identifierListAt(
      candidate.keep_site_ids,
      "site_similarity_decision.keep_site_ids",
    ),
    decisionVersion: version(
      candidate.decision_version,
      "site_similarity_decision.decision_version",
    ),
    selectedGroupCount: count(
      candidate.selected_group_count,
      "site_similarity_decision.selected_group_count",
    ),
    selectedDeleteCount: count(
      candidate.selected_delete_count,
      "site_similarity_decision.selected_delete_count",
    ),
  };
}

export function normalizeSiteSimilarityRecommendedDecision(
  value: unknown,
): SiteSimilarityRecommendedDecisionResult {
  const candidate = record(value, "site_similarity_recommended_decision");
  const matchedGroupCount = count(
    candidate.matched_group_count,
    "site_similarity_recommended_decision.matched_group_count",
  );
  const updatedGroupCount = count(
    candidate.updated_group_count,
    "site_similarity_recommended_decision.updated_group_count",
  );
  if (updatedGroupCount > matchedGroupCount) {
    throw new LibraryContractError("批量更新分组数不能超过当前分区分组数");
  }
  const selectedGroupCount = count(
    candidate.selected_group_count,
    "site_similarity_recommended_decision.selected_group_count",
  );
  const selectedDeleteCount = count(
    candidate.selected_delete_count,
    "site_similarity_recommended_decision.selected_delete_count",
  );
  if (
    selectedGroupCount < matchedGroupCount
    || selectedDeleteCount < selectedGroupCount
  ) {
    throw new LibraryContractError("批量推荐后的清理选择计数不一致");
  }
  return {
    kind: literal(
      candidate.kind,
      "site_similarity_recommended_decision.kind",
      SITE_SIMILARITY_GROUP_FILTERS,
    ),
    matchedGroupCount,
    updatedGroupCount,
    decisionVersion: version(
      candidate.decision_version,
      "site_similarity_recommended_decision.decision_version",
    ),
    selectedGroupCount,
    selectedDeleteCount,
  };
}

export function normalizeSiteSimilarityApply(value: unknown): SiteSimilarityApplyResult {
  const candidate = record(value, "site_similarity_apply");
  if (candidate.status !== "applied") {
    throw new LibraryContractError("site_similarity_apply.status 不是 applied");
  }
  if (!Array.isArray(candidate.deleted_site_ids) || !Array.isArray(candidate.kept_site_ids)) {
    throw new LibraryContractError("site_similarity_apply 的网站 ID 必须是数组");
  }
  const deletedSiteIds = candidate.deleted_site_ids.map((siteId, index) =>
    identifier(siteId, `site_similarity_apply.deleted_site_ids[${index}]`));
  const keptSiteIds = candidate.kept_site_ids.map((siteId, index) =>
    identifier(siteId, `site_similarity_apply.kept_site_ids[${index}]`));
  const deletedSiteCount = count(
    candidate.deleted_site_count,
    "site_similarity_apply.deleted_site_count",
  );
  const keptSiteCount = count(candidate.kept_site_count, "site_similarity_apply.kept_site_count");
  if (deletedSiteCount !== deletedSiteIds.length || keptSiteCount !== keptSiteIds.length) {
    throw new LibraryContractError("site_similarity_apply 的网站计数与 ID 列表不一致");
  }
  const uniqueDeleted = new Set(deletedSiteIds);
  if (
    uniqueDeleted.size !== deletedSiteIds.length
    || new Set(keptSiteIds).size !== keptSiteIds.length
    || keptSiteIds.some((siteId) => uniqueDeleted.has(siteId))
  ) {
    throw new LibraryContractError("site_similarity_apply 的网站 ID 重复或互相冲突");
  }
  return {
    runId: identifier(candidate.id, "site_similarity_apply.id"),
    status: "applied",
    decisionVersion: version(
      candidate.decision_version,
      "site_similarity_apply.decision_version",
    ),
    mergedGroupCount: count(
      candidate.merged_group_count,
      "site_similarity_apply.merged_group_count",
    ),
    deletedSiteCount,
    keptSiteCount,
    deletedSiteIds,
    keptSiteIds,
    appliedAt: isoDate(candidate.applied_at, "site_similarity_apply.applied_at"),
  };
}

export function assertMetadataBackfillRequest(
  value: MetadataBackfillRequest,
): MetadataBackfillRequest {
  const mode = literal(value.mode, "metadata_backfill_request.mode", METADATA_BACKFILL_MODES);
  const limit = version(value.limit, "metadata_backfill_request.limit");
  const maximum = METADATA_BACKFILL_LIMITS[mode].maxLimit;
  if (limit > maximum) {
    throw new LibraryContractError(
      `metadata_backfill_request.limit 在 ${mode} 模式下不能超过 ${maximum}`,
    );
  }
  return { mode, limit };
}

export function normalizeMetadataBackfillPlan(value: unknown): MetadataBackfillPlan {
  const candidate = record(value, "metadata_backfill_plan");
  const mode = literal(candidate.mode, "metadata_backfill_plan.mode", METADATA_BACKFILL_MODES);
  const requestedLimit = version(
    candidate.requested_limit,
    "metadata_backfill_plan.requested_limit",
  );
  const eligibleCount = count(candidate.eligible_count, "metadata_backfill_plan.eligible_count");
  const selectedCount = count(candidate.selected_count, "metadata_backfill_plan.selected_count");
  const llmCount = count(candidate.llm_count, "metadata_backfill_plan.llm_count");
  const maxLimit = version(candidate.max_limit, "metadata_backfill_plan.max_limit");
  const clientMaximum = METADATA_BACKFILL_LIMITS[mode].maxLimit;

  if (maxLimit > clientMaximum) {
    throw new LibraryContractError("metadata_backfill_plan.max_limit 超出前端支持范围");
  }
  if (requestedLimit > maxLimit) {
    throw new LibraryContractError("metadata_backfill_plan.requested_limit 不能超过 max_limit");
  }
  if (selectedCount > requestedLimit || selectedCount > eligibleCount) {
    throw new LibraryContractError("metadata_backfill_plan.selected_count 超出可选范围");
  }
  if (llmCount > selectedCount) {
    throw new LibraryContractError("metadata_backfill_plan.llm_count 不能超过 selected_count");
  }
  if (mode === "metadata" && llmCount !== 0) {
    throw new LibraryContractError("metadata 模式不能包含 LLM 调用");
  }

  return {
    mode,
    requestedLimit,
    eligibleCount,
    selectedCount,
    llmCount,
    maxLimit,
  };
}

export function normalizeLibraryBulkDeleteResult(value: unknown): LibraryBulkDeleteResult {
  const candidate = record(value, "bulk_delete");
  if (!Array.isArray(candidate.deleted_site_ids)) {
    throw new LibraryContractError("bulk_delete.deleted_site_ids 必须是数组");
  }
  const deletedSiteIds = candidate.deleted_site_ids.map((siteId, index) =>
    identifier(siteId, `bulk_delete.deleted_site_ids[${index}]`));
  if (new Set(deletedSiteIds).size !== deletedSiteIds.length) {
    throw new LibraryContractError("bulk_delete.deleted_site_ids 不能包含重复网站");
  }
  return { deletedSiteIds };
}

export function normalizeLibraryCategories(value: unknown): LibraryCategory[] {
  return listPayload(value, "categories").map(normalizeLibraryCategory);
}

export function normalizeLibraryTags(value: unknown): LibraryTag[] {
  return listPayload(value, "tags").map(normalizeLibraryTag);
}

export function normalizeLibrarySitePage(value: unknown): LibrarySitePage {
  const candidate = record(value, "sites");
  if (!Array.isArray(candidate.items)) throw new LibraryContractError("sites.items 必须是数组");
  const aggregate = record(candidate.aggregate, "sites.aggregate");
  const nextCursor = candidate.next_cursor;
  if (nextCursor !== null && (typeof nextCursor !== "string" || !nextCursor.trim())) {
    throw new LibraryContractError("sites.next_cursor 必须是非空字符串或 null");
  }

  return {
    items: candidate.items.map(normalizeLibrarySite),
    nextCursor: typeof nextCursor === "string" ? nextCursor.trim() : null,
    aggregate: {
      matchedCount: count(aggregate.matched_count, "sites.aggregate.matched_count"),
      pinnedCount: count(aggregate.pinned_count, "sites.aggregate.pinned_count"),
    },
  };
}

export function normalizeCategoryDeletePreview(value: unknown): LibraryCategoryDeletePreview {
  const candidate = record(value, "delete_preview");
  return {
    category: normalizeLibraryCategory(candidate.category),
    affectedSiteCount: count(candidate.affected_site_count, "delete_preview.affected_site_count"),
    replacementCategory:
      candidate.replacement_category === null
        ? null
        : normalizeLibraryCategory(candidate.replacement_category),
  };
}

function optionalErrorText(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function fallbackLibraryErrorMessage(status: number): string {
  if (status === 401) return "登录状态已失效，请重新登录";
  if (status === 404) return "请求的网址库内容不存在";
  if (status === 409) return "内容已被更新，请刷新后重试";
  if (status === 422) return "提交的信息不符合要求";
  if (status === 429) return "操作过于频繁，请稍后重试";
  return "网址库服务暂时不可用，请稍后重试";
}

export function libraryErrorDetails(status: number, payload: unknown): LibraryErrorDetails {
  let code: string | undefined;
  let message: string | undefined;

  if (typeof payload === "object" && payload !== null && !Array.isArray(payload)) {
    const candidate = payload as JsonRecord;
    code = optionalErrorText(candidate.code);
    message = optionalErrorText(candidate.message);

    if (typeof candidate.detail === "object" && candidate.detail !== null && !Array.isArray(candidate.detail)) {
      const detail = candidate.detail as JsonRecord;
      code = optionalErrorText(detail.code) ?? code;
      message = optionalErrorText(detail.message) ?? message;
    } else if (typeof candidate.detail === "string") {
      message = optionalErrorText(candidate.detail) ?? message;
    }

    if (Array.isArray(candidate.detail)) {
      const first = candidate.detail.find(
        (entry) => typeof entry === "object" && entry !== null && !Array.isArray(entry),
      ) as JsonRecord | undefined;
      message = optionalErrorText(first?.msg) ?? message;
    }
  }

  // FastAPI's router-level 404 means the browser is talking to an API process
  // that does not contain this client feature.  It is different from a
  // structured business 404 such as `site_not_found`, so make the deployment
  // mismatch actionable instead of leaking the framework's bare "Not Found".
  if (status === 404 && code === undefined && message === "Not Found") {
    message = "当前运行的 API 版本过旧，请完成数据库升级并重启 API 服务";
  }

  return {
    ...(code ? { code } : {}),
    message: message ?? fallbackLibraryErrorMessage(status),
  };
}

export function libraryErrorMessage(status: number, payload: unknown): string {
  return libraryErrorDetails(status, payload).message;
}

export function assertLibrarySiteCreateSource(value: unknown): LibrarySiteCreateSource {
  if (!(LIBRARY_SITE_CREATE_SOURCES as readonly unknown[]).includes(value)) {
    throw new LibraryContractError("site.source 只支持 manual 或 agent");
  }
  return value as LibrarySiteCreateSource;
}

export function assertLibrarySiteCreateInput(input: LibrarySiteCreateInput): LibrarySiteCreateInput {
  const name = boundedText(input.name, "site.name", MAX_LIBRARY_SITE_NAME_LENGTH);
  const url = absoluteWebUrl(input.url, "site.url");
  const summary = input.summary === undefined
    ? undefined
    : assertedSiteSummary(input.summary) ?? undefined;
  const description = input.description === undefined
    ? undefined
    : nullableText(input.description, "site.description") ?? undefined;
  const faviconUrl = input.faviconUrl === undefined ? undefined : nullableWebUrl(input.faviconUrl, "site.favicon_url");
  const categoryId = input.categoryId === undefined ? undefined : identifier(input.categoryId, "site.category_id");
  const tagIds = input.tagIds === undefined
    ? undefined
    : Array.from(new Set(input.tagIds.map((id, index) => identifier(id, `site.tag_ids[${index}]`))));
  if (input.pinned !== undefined && typeof input.pinned !== "boolean") {
    throw new LibraryContractError("site.pinned 必须是布尔值");
  }
  const source = input.source === undefined
    ? undefined
    : assertLibrarySiteCreateSource(input.source);
  return {
    name,
    url,
    summary,
    description,
    faviconUrl,
    categoryId,
    tagIds,
    pinned: input.pinned,
    ...(source !== undefined ? { source } : {}),
  };
}

export function assertLibrarySiteUpdateInput(input: LibrarySiteUpdateInput): LibrarySiteUpdateInput {
  const expectedVersion = version(input.expectedVersion, "site.expected_version");
  const editable: Partial<LibrarySiteUpdateInput> = { ...input };
  delete editable.expectedVersion;
  const populated = Object.values(editable).some((value) => value !== undefined);
  if (!populated) throw new LibraryContractError("站点更新至少需要一个字段");

  return {
    expectedVersion,
    ...(input.name !== undefined
      ? { name: boundedText(input.name, "site.name", MAX_LIBRARY_SITE_NAME_LENGTH) }
      : {}),
    ...(input.url !== undefined ? { url: absoluteWebUrl(input.url, "site.url") } : {}),
    ...(input.summary !== undefined
      ? { summary: assertedSiteSummary(input.summary) }
      : {}),
    ...(input.description !== undefined
      ? { description: nullableText(input.description, "site.description") }
      : {}),
    ...(input.faviconUrl !== undefined
      ? { faviconUrl: nullableWebUrl(input.faviconUrl, "site.favicon_url") }
      : {}),
    ...(input.categoryId !== undefined
      ? { categoryId: input.categoryId === null ? null : identifier(input.categoryId, "site.category_id") }
      : {}),
    ...(input.tagIds !== undefined
      ? { tagIds: Array.from(new Set(input.tagIds.map((id, index) => identifier(id, `site.tag_ids[${index}]`)))) }
      : {}),
    ...(input.pinned !== undefined ? { pinned: boolean(input.pinned, "site.pinned") } : {}),
  };
}

export function assertLibraryExpectedVersion(value: unknown): number {
  return version(value, "site.expected_version");
}

export function assertLibraryBulkDeleteItems(
  items: LibraryBulkDeleteItem[],
): LibraryBulkDeleteItem[] {
  if (!Array.isArray(items) || items.length < 1) {
    throw new LibraryContractError("批量删除至少需要一个网站");
  }
  if (items.length > MAX_LIBRARY_BULK_DELETE_SITES) {
    throw new LibraryContractError(`单次最多删除 ${MAX_LIBRARY_BULK_DELETE_SITES} 个网站`);
  }
  const normalized = items.map((item, index) => ({
    siteId: identifier(item.siteId, `bulk_delete.items[${index}].site_id`),
    expectedVersion: version(
      item.expectedVersion,
      `bulk_delete.items[${index}].expected_version`,
    ),
  }));
  if (new Set(normalized.map((item) => item.siteId)).size !== normalized.length) {
    throw new LibraryContractError("批量删除不能包含重复网站");
  }
  return normalized;
}

export function assertLibraryCategoryName(name: string): string {
  return boundedText(name, "category.name", MAX_LIBRARY_CATEGORY_NAME_LENGTH);
}

export function assertLibraryTagName(name: string): string {
  return boundedText(
    normalizeLibraryTagName(name),
    "tag.name",
    MAX_LIBRARY_TAG_NAME_LENGTH,
  );
}
