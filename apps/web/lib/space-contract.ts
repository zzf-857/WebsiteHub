import { createContractGuards } from "./contract-guards.ts";

export type SpaceSort = "created" | "updated" | "name";
export type SpaceDirection = "asc" | "desc";

export const MAX_SPACE_NAME_LENGTH = 120;
export const MAX_SPACE_SITE_ID_LENGTH = 100;
export const MAX_SPACE_REORDER_MEMBER_COUNT = 100;
export const MAX_SPACE_CURSOR_LENGTH = 2_048;

export type Space = {
  id: string;
  name: string;
  memberCount: number;
  version: number;
  createdAt: string;
  updatedAt: string;
};

export type SpacePage = {
  items: Space[];
  nextCursor: string | null;
  aggregate: {
    totalCount: number;
  };
};

export type SpaceSiteReference = {
  id: string;
  name: string;
  originalUrl: string;
  identityUrl: string;
  description: string;
  faviconUrl: string | null;
  pinned: boolean;
  version: number;
};

export type SpaceMember = {
  site: SpaceSiteReference;
  position: number;
  addedAt: string;
};

export type SpaceDetail = Space & {
  members: SpaceMember[];
  nextCursor: string | null;
};

export type SpaceMemberAddResult = {
  space: Space;
  member: SpaceMember;
};

export type SpaceMemberDeleteResult = {
  message: string;
  spaceId: string;
  siteId: string;
  memberCount: number;
  version: number;
};

export type SpaceDeletePreview = {
  space: Space;
  affectedSiteCount: number;
};

export type SpaceDeleteResult = {
  message: string;
  spaceId: string;
  unlinkedSiteCount: number;
};

export type SpaceListQuery = {
  sort?: SpaceSort;
  direction?: SpaceDirection;
  cursor?: string;
  limit?: number;
};

export type SpaceDetailQuery = {
  cursor?: string;
  limit?: number;
};

export type SpaceCreateInput = {
  name: string;
};

export type SpaceUpdateInput = {
  expectedVersion: number;
  name: string;
};

export type SpaceMemberAddInput = {
  expectedVersion: number;
  siteId: string;
};

export type SpaceReorderInput = {
  expectedVersion: number;
  orderedSiteIds: string[];
  beforeSiteId?: string | null;
};

export type SpaceErrorDetails = {
  code?: string;
  message: string;
};

type JsonRecord = Record<string, unknown>;

export class SpaceContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SpaceContractError";
  }
}

// 校验原语与其他契约模块完全一致，统一放在 contract-guards；
// 这里只绑定本模块自己的错误类型，便于调用方按类型区分来源。
const {
  record,
  text,
  boundedText,
  boolean,
  count,
  version,
  absoluteWebUrl,
  nullableWebUrl,
  isoDate,
} = createContractGuards((message) => new SpaceContractError(message));

// identifier 刻意不用共享版：Space 的成员 id 有长度上限，且**不接受数字**
// （library/provider 的共享版为兼容历史整数主键会把数字转成字符串）。
// 同名不等于同一件事，强行合并会悄悄放宽这里的约束。
function identifier(value: unknown, path: string, maximum?: number): string {
  const candidate = text(value, path);
  if (maximum !== undefined && Array.from(candidate).length > maximum) {
    throw new SpaceContractError(`${path} 不能超过 ${maximum} 个字符`);
  }
  return candidate;
}

function stringValue(value: unknown, path: string): string {
  if (typeof value !== "string") throw new SpaceContractError(`${path} 必须是字符串`);
  return value;
}

function cursor(value: unknown, path: string): string | null {
  if (value === null) return null;
  const candidate = boundedText(value, path, MAX_SPACE_CURSOR_LENGTH);
  return candidate;
}

function normalizeSpaceAt(value: unknown, path: string): Space {
  const candidate = record(value, path);
  return {
    id: identifier(candidate.id, `${path}.id`),
    name: text(candidate.name, `${path}.name`),
    memberCount: count(candidate.member_count, `${path}.member_count`),
    version: version(candidate.version, `${path}.version`),
    createdAt: isoDate(candidate.created_at, `${path}.created_at`),
    updatedAt: isoDate(candidate.updated_at, `${path}.updated_at`),
  };
}

function normalizeSiteReferenceAt(value: unknown, path: string): SpaceSiteReference {
  const candidate = record(value, path);
  return {
    id: identifier(candidate.id, `${path}.id`),
    name: text(candidate.name, `${path}.name`),
    originalUrl: absoluteWebUrl(candidate.original_url, `${path}.original_url`),
    identityUrl: absoluteWebUrl(candidate.identity_url, `${path}.identity_url`),
    description: stringValue(candidate.description, `${path}.description`),
    faviconUrl: nullableWebUrl(candidate.favicon_url, `${path}.favicon_url`),
    pinned: boolean(candidate.pinned, `${path}.pinned`),
    version: version(candidate.version, `${path}.version`),
  };
}

function normalizeMemberAt(value: unknown, path: string): SpaceMember {
  const candidate = record(value, path);
  return {
    site: normalizeSiteReferenceAt(candidate.site, `${path}.site`),
    position: count(candidate.position, `${path}.position`),
    addedAt: isoDate(candidate.added_at, `${path}.added_at`),
  };
}

export function normalizeSpace(value: unknown): Space {
  return normalizeSpaceAt(value, "space");
}

export function normalizeSpacePage(value: unknown): SpacePage {
  const candidate = record(value, "spaces");
  if (!Array.isArray(candidate.items)) {
    throw new SpaceContractError("spaces.items 必须是数组");
  }
  const aggregate = record(candidate.aggregate, "spaces.aggregate");
  return {
    items: candidate.items.map((item, index) => normalizeSpaceAt(item, `spaces.items[${index}]`)),
    nextCursor: cursor(candidate.next_cursor, "spaces.next_cursor"),
    aggregate: {
      totalCount: count(aggregate.total_count, "spaces.aggregate.total_count"),
    },
  };
}

export function normalizeSpaceMember(value: unknown): SpaceMember {
  return normalizeMemberAt(value, "member");
}

export function normalizeSpaceDetail(value: unknown): SpaceDetail {
  const candidate = record(value, "space");
  if (!Array.isArray(candidate.members)) {
    throw new SpaceContractError("space.members 必须是数组");
  }
  return {
    ...normalizeSpaceAt(candidate, "space"),
    members: candidate.members.map((member, index) =>
      normalizeMemberAt(member, `space.members[${index}]`),
    ),
    nextCursor: cursor(candidate.next_cursor, "space.next_cursor"),
  };
}

export function normalizeSpaceMemberAddResult(value: unknown): SpaceMemberAddResult {
  const candidate = record(value, "member_add");
  return {
    space: normalizeSpaceAt(candidate.space, "member_add.space"),
    member: normalizeMemberAt(candidate.member, "member_add.member"),
  };
}

export function normalizeSpaceMemberDeleteResult(value: unknown): SpaceMemberDeleteResult {
  const candidate = record(value, "member_delete");
  return {
    message: text(candidate.message, "member_delete.message"),
    spaceId: identifier(candidate.space_id, "member_delete.space_id"),
    siteId: identifier(candidate.site_id, "member_delete.site_id"),
    memberCount: count(candidate.member_count, "member_delete.member_count"),
    version: version(candidate.version, "member_delete.version"),
  };
}

export function normalizeSpaceDeletePreview(value: unknown): SpaceDeletePreview {
  const candidate = record(value, "delete_preview");
  return {
    space: normalizeSpaceAt(candidate.space, "delete_preview.space"),
    affectedSiteCount: count(
      candidate.affected_site_count,
      "delete_preview.affected_site_count",
    ),
  };
}

export function normalizeSpaceDeleteResult(value: unknown): SpaceDeleteResult {
  const candidate = record(value, "space_delete");
  return {
    message: text(candidate.message, "space_delete.message"),
    spaceId: identifier(candidate.space_id, "space_delete.space_id"),
    unlinkedSiteCount: count(
      candidate.unlinked_site_count,
      "space_delete.unlinked_site_count",
    ),
  };
}

function optionalErrorText(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function fallbackSpaceErrorMessage(status: number): string {
  if (status === 401) return "登录状态已失效，请重新登录";
  if (status === 403) return "当前请求不被允许，请刷新页面后重试";
  if (status === 404) return "请求的 Space 或网站不存在";
  if (status === 409) return "Space 已被更新，请刷新后重试";
  if (status === 422) return "提交的 Space 信息不符合要求";
  if (status === 429) return "操作过于频繁，请稍后重试";
  return "Space 服务暂时不可用，请稍后重试";
}

export function spaceErrorDetails(status: number, payload: unknown): SpaceErrorDetails {
  let code: string | undefined;
  let message: string | undefined;

  if (typeof payload === "object" && payload !== null && !Array.isArray(payload)) {
    const candidate = payload as JsonRecord;
    code = optionalErrorText(candidate.code);
    message = optionalErrorText(candidate.message);

    if (
      typeof candidate.detail === "object"
      && candidate.detail !== null
      && !Array.isArray(candidate.detail)
    ) {
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
    message: message ?? fallbackSpaceErrorMessage(status),
  };
}

export function spaceErrorMessage(status: number, payload: unknown): string {
  return spaceErrorDetails(status, payload).message;
}

export function assertSpaceName(name: string): string {
  if (typeof name !== "string") throw new SpaceContractError("space.name 必须是字符串");
  const normalized = name.normalize("NFKC").trim().split(/\s+/u).filter(Boolean).join(" ");
  return boundedText(normalized, "space.name", MAX_SPACE_NAME_LENGTH);
}

export function assertSpaceExpectedVersion(value: number): number {
  return version(value, "space.expected_version");
}

export function assertSpaceCreateInput(input: SpaceCreateInput): SpaceCreateInput {
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    throw new SpaceContractError("space 必须是对象");
  }
  return { name: assertSpaceName(input.name) };
}

export function assertSpaceUpdateInput(input: SpaceUpdateInput): SpaceUpdateInput {
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    throw new SpaceContractError("space 必须是对象");
  }
  return {
    expectedVersion: assertSpaceExpectedVersion(input.expectedVersion),
    name: assertSpaceName(input.name),
  };
}

export function assertSpaceMemberAddInput(input: SpaceMemberAddInput): SpaceMemberAddInput {
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    throw new SpaceContractError("member 必须是对象");
  }
  return {
    expectedVersion: assertSpaceExpectedVersion(input.expectedVersion),
    siteId: identifier(input.siteId, "member.site_id", MAX_SPACE_SITE_ID_LENGTH),
  };
}

export function assertSpaceReorderInput(input: SpaceReorderInput): SpaceReorderInput {
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    throw new SpaceContractError("reorder 必须是对象");
  }
  if (!Array.isArray(input.orderedSiteIds)) {
    throw new SpaceContractError("reorder.ordered_site_ids 必须是数组");
  }
  if (input.orderedSiteIds.length < 1) {
    throw new SpaceContractError("reorder.ordered_site_ids 至少需要一个成员");
  }
  if (input.orderedSiteIds.length > MAX_SPACE_REORDER_MEMBER_COUNT) {
    throw new SpaceContractError(
      `reorder.ordered_site_ids 不能超过 ${MAX_SPACE_REORDER_MEMBER_COUNT} 个成员`,
    );
  }

  const orderedSiteIds = input.orderedSiteIds.map((siteId, index) =>
    identifier(siteId, `reorder.ordered_site_ids[${index}]`, MAX_SPACE_SITE_ID_LENGTH),
  );
  if (new Set(orderedSiteIds).size !== orderedSiteIds.length) {
    throw new SpaceContractError("排序成员不能重复");
  }

  const beforeSiteId = input.beforeSiteId === undefined
    ? undefined
    : input.beforeSiteId === null
      ? null
      : identifier(input.beforeSiteId, "reorder.before_site_id", MAX_SPACE_SITE_ID_LENGTH);
  if (beforeSiteId !== undefined && beforeSiteId !== null && orderedSiteIds.includes(beforeSiteId)) {
    throw new SpaceContractError("定位成员不能同时出现在移动列表中");
  }

  return {
    expectedVersion: assertSpaceExpectedVersion(input.expectedVersion),
    orderedSiteIds,
    ...(input.beforeSiteId !== undefined ? { beforeSiteId } : {}),
  };
}
