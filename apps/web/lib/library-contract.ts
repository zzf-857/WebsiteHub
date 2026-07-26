export type LibrarySort = "created" | "updated" | "name";
export type LibraryDirection = "asc" | "desc";

const LIBRARY_SITE_SOURCES = ["manual", "agent", "browser_import", "backup"] as const;
const LIBRARY_ANALYSIS_STATUSES = ["not_analyzed", "pending", "complete", "failed", "limited"] as const;

export type LibrarySiteSource = (typeof LIBRARY_SITE_SOURCES)[number];
export type LibraryAnalysisStatus = (typeof LIBRARY_ANALYSIS_STATUSES)[number];

export const MAX_LIBRARY_SITE_NAME_LENGTH = 160;
export const MAX_LIBRARY_CATEGORY_NAME_LENGTH = 80;
export const MAX_LIBRARY_TAG_NAME_LENGTH = 40;

export type LibraryCategoryRef = {
  id: string;
  name: string;
  isDefault: boolean;
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
  description: string | null;
  faviconUrl: string | null;
  category: LibraryCategoryRef;
  tags: LibraryTagRef[];
  pinned: boolean;
  source: LibrarySiteSource;
  analysisStatus: LibraryAnalysisStatus;
  version: number;
  createdAt: string;
  updatedAt: string;
};

export type LibrarySitePage = {
  items: LibrarySite[];
  nextCursor: string | null;
  aggregate: {
    matchedCount: number;
    pinnedCount: number;
  };
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
  description?: string | null;
  faviconUrl?: string | null;
  categoryId?: string;
  tagIds?: string[];
  pinned?: boolean;
};

export type LibrarySiteUpdateInput = Omit<Partial<LibrarySiteCreateInput>, "categoryId"> & {
  categoryId?: string | null;
  expectedVersion: number;
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

function record(value: unknown, path: string): JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new LibraryContractError(`${path} 必须是对象`);
  }
  return value as JsonRecord;
}

function text(value: unknown, path: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new LibraryContractError(`${path} 必须是非空字符串`);
  }
  return value.trim();
}

function boundedText(value: unknown, path: string, maximum: number): string {
  const candidate = text(value, path);
  if (Array.from(candidate).length > maximum) {
    throw new LibraryContractError(`${path} 不能超过 ${maximum} 个字符`);
  }
  return candidate;
}

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

function nullableText(value: unknown, path: string): string | null {
  if (value === null) return null;
  if (typeof value !== "string") throw new LibraryContractError(`${path} 必须是字符串或 null`);
  return value.trim() || null;
}

function identifier(value: unknown, path: string): string {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return String(value);
  throw new LibraryContractError(`${path} 必须是有效标识符`);
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw new LibraryContractError(`${path} 必须是布尔值`);
  return value;
}

function count(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new LibraryContractError(`${path} 必须是非负整数`);
  }
  return value as number;
}

function version(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) {
    throw new LibraryContractError(`${path} 必须是正整数`);
  }
  return value as number;
}

function absoluteWebUrl(value: unknown, path: string): string {
  const candidate = text(value, path);
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("protocol");
  } catch {
    throw new LibraryContractError(`${path} 必须是 HTTP(S) URL`);
  }
  return candidate;
}

function nullableWebUrl(value: unknown, path: string): string | null {
  if (value === null) return null;
  return absoluteWebUrl(value, path);
}

function isoDate(value: unknown, path: string): string {
  const candidate = text(value, path);
  if (Number.isNaN(Date.parse(candidate))) throw new LibraryContractError(`${path} 必须是有效日期`);
  return candidate;
}

function normalizeCategoryRefAt(value: unknown, path: string): LibraryCategoryRef {
  const candidate = record(value, path);
  return {
    id: identifier(candidate.id, `${path}.id`),
    name: text(candidate.name, `${path}.name`),
    isDefault: boolean(candidate.is_default, `${path}.is_default`),
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
    description: nullableText(candidate.description, "site.description"),
    faviconUrl: nullableWebUrl(candidate.favicon_url, "site.favicon_url"),
    category: normalizeCategoryRefAt(candidate.category, "site.category"),
    tags: candidate.tags.map((tag, index) => normalizeTagRefAt(tag, `site.tags[${index}]`)),
    pinned: boolean(candidate.pinned, "site.pinned"),
    source: literal(candidate.source, "site.source", LIBRARY_SITE_SOURCES),
    analysisStatus: literal(candidate.analysis_status, "site.analysis_status", LIBRARY_ANALYSIS_STATUSES),
    version: version(candidate.version, "site.version"),
    createdAt: isoDate(candidate.created_at, "site.created_at"),
    updatedAt: isoDate(candidate.updated_at, "site.updated_at"),
  };
}

function listPayload(value: unknown, path: string): unknown[] {
  if (Array.isArray(value)) return value;
  const candidate = record(value, path);
  if (!Array.isArray(candidate.items)) throw new LibraryContractError(`${path}.items 必须是数组`);
  return candidate.items;
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
  if (status === 404) return "请求的资料库内容不存在";
  if (status === 409) return "内容已被更新，请刷新后重试";
  if (status === 422) return "提交的信息不符合要求";
  if (status === 429) return "操作过于频繁，请稍后重试";
  return "资料库服务暂时不可用，请稍后重试";
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

  return {
    ...(code ? { code } : {}),
    message: message ?? fallbackLibraryErrorMessage(status),
  };
}

export function libraryErrorMessage(status: number, payload: unknown): string {
  return libraryErrorDetails(status, payload).message;
}

export function assertLibrarySiteCreateInput(input: LibrarySiteCreateInput): LibrarySiteCreateInput {
  const name = boundedText(input.name, "site.name", MAX_LIBRARY_SITE_NAME_LENGTH);
  const url = absoluteWebUrl(input.url, "site.url");
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
  return { name, url, description, faviconUrl, categoryId, tagIds, pinned: input.pinned };
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

export function assertLibraryCategoryName(name: string): string {
  return boundedText(name, "category.name", MAX_LIBRARY_CATEGORY_NAME_LENGTH);
}

export function assertLibraryTagName(name: string): string {
  return boundedText(name, "tag.name", MAX_LIBRARY_TAG_NAME_LENGTH);
}
