import {
  assertSpaceCreateInput,
  assertSpaceExpectedVersion,
  assertSpaceMemberBatchInput,
  assertSpaceMemberAddInput,
  assertSpaceReorderInput,
  assertSpaceUpdateInput,
  MAX_SPACE_CURSOR_LENGTH,
  normalizeSpace,
  normalizeSpaceDeletePreview,
  normalizeSpaceDeleteResult,
  normalizeSpaceDetail,
  normalizeSpaceMemberAddResult,
  normalizeSpaceMemberBatchResult,
  normalizeSpaceMemberDeleteResult,
  normalizeSpacePage,
  spaceErrorDetails,
  SpaceContractError,
  type Space,
  type SpaceCreateInput,
  type SpaceDeletePreview,
  type SpaceDeleteResult,
  type SpaceDetail,
  type SpaceDetailQuery,
  type SpaceListQuery,
  type SpaceMemberAddInput,
  type SpaceMemberAddResult,
  type SpaceMemberBatchInput,
  type SpaceMemberBatchResult,
  type SpaceMemberDeleteResult,
  type SpacePage,
  type SpaceReorderInput,
  type SpaceUpdateInput,
} from "./space-contract.ts";

const SPACE_BASE = "/api/backend/spaces";
export const DEFAULT_SPACE_PAGE_SIZE = 20;
export const DEFAULT_SPACE_MEMBER_PAGE_SIZE = 50;
export const MAX_SPACE_PAGE_SIZE = 100;

export class SpaceApiError extends Error {
  status: number;
  code?: string;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = "SpaceApiError";
    this.status = status;
    if (code) this.code = code;
  }
}

async function readJson(response: Response): Promise<unknown> {
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) return null;
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function request(path: string, init: RequestInit = {}): Promise<unknown> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${SPACE_BASE}${path}`, {
    ...init,
    cache: "no-store",
    credentials: "include",
    headers,
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const error = spaceErrorDetails(response.status, payload);
    throw new SpaceApiError(response.status, error.message, error.code);
  }
  return payload;
}

function encodeId(id: string): string {
  if (typeof id !== "string") throw new SpaceContractError("资源 ID 必须是字符串");
  const normalized = id.trim();
  if (!normalized) throw new SpaceContractError("资源 ID 不能为空");
  return encodeURIComponent(normalized);
}

function normalizedLimit(
  value: number | undefined,
  fallback: number,
  path: string,
): number {
  if (value === undefined) return fallback;
  if (!Number.isFinite(value)) throw new SpaceContractError(`${path} 必须是有限数字`);
  return Math.min(MAX_SPACE_PAGE_SIZE, Math.max(1, Math.trunc(value)));
}

function normalizedCursor(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string") throw new SpaceContractError("cursor 必须是字符串");
  const normalized = value.trim();
  if (!normalized) return undefined;
  if (Array.from(normalized).length > MAX_SPACE_CURSOR_LENGTH) {
    throw new SpaceContractError(`cursor 不能超过 ${MAX_SPACE_CURSOR_LENGTH} 个字符`);
  }
  return normalized;
}

export function buildSpaceListSearchParams(query: SpaceListQuery = {}): URLSearchParams {
  const sort = query.sort ?? "updated";
  const direction = query.direction ?? "desc";
  if (sort !== "created" && sort !== "updated" && sort !== "name") {
    throw new SpaceContractError("sort 不是受支持的值");
  }
  if (direction !== "asc" && direction !== "desc") {
    throw new SpaceContractError("direction 不是受支持的值");
  }

  const params = new URLSearchParams({ sort, direction });
  const cursor = normalizedCursor(query.cursor);
  if (cursor) params.set("cursor", cursor);
  params.set("limit", String(normalizedLimit(query.limit, DEFAULT_SPACE_PAGE_SIZE, "limit")));
  return params;
}

export function buildSpaceDetailSearchParams(query: SpaceDetailQuery = {}): URLSearchParams {
  const params = new URLSearchParams();
  const cursor = normalizedCursor(query.cursor);
  if (cursor) params.set("cursor", cursor);
  params.set(
    "limit",
    String(normalizedLimit(query.limit, DEFAULT_SPACE_MEMBER_PAGE_SIZE, "limit")),
  );
  return params;
}

export async function listSpaces(
  query: SpaceListQuery = {},
  signal?: AbortSignal,
): Promise<SpacePage> {
  const params = buildSpaceListSearchParams(query);
  return normalizeSpacePage(await request(`?${params.toString()}`, { signal }));
}

export async function createSpace(
  input: SpaceCreateInput,
  signal?: AbortSignal,
): Promise<Space> {
  const normalized = assertSpaceCreateInput(input);
  return normalizeSpace(await request("", {
    method: "POST",
    body: JSON.stringify({ name: normalized.name }),
    signal,
  }));
}

export async function getSpace(
  id: string,
  query: SpaceDetailQuery = {},
  signal?: AbortSignal,
): Promise<SpaceDetail> {
  const params = buildSpaceDetailSearchParams(query);
  return normalizeSpaceDetail(
    await request(`/${encodeId(id)}?${params.toString()}`, { signal }),
  );
}

export async function updateSpace(
  id: string,
  input: SpaceUpdateInput,
  signal?: AbortSignal,
): Promise<Space> {
  const normalized = assertSpaceUpdateInput(input);
  return normalizeSpace(await request(`/${encodeId(id)}`, {
    method: "PATCH",
    body: JSON.stringify({
      expected_version: normalized.expectedVersion,
      name: normalized.name,
    }),
    signal,
  }));
}

export async function previewSpaceDelete(
  id: string,
  signal?: AbortSignal,
): Promise<SpaceDeletePreview> {
  return normalizeSpaceDeletePreview(
    await request(`/${encodeId(id)}/delete-preview`, { signal }),
  );
}

export async function deleteSpace(
  id: string,
  expectedVersion: number,
  signal?: AbortSignal,
): Promise<SpaceDeleteResult> {
  const params = new URLSearchParams({
    expected_version: String(assertSpaceExpectedVersion(expectedVersion)),
  });
  return normalizeSpaceDeleteResult(await request(`/${encodeId(id)}?${params.toString()}`, {
    method: "DELETE",
    signal,
  }));
}

export async function addSpaceMember(
  spaceId: string,
  input: SpaceMemberAddInput,
  signal?: AbortSignal,
): Promise<SpaceMemberAddResult> {
  const normalized = assertSpaceMemberAddInput(input);
  return normalizeSpaceMemberAddResult(await request(`/${encodeId(spaceId)}/members`, {
    method: "POST",
    body: JSON.stringify({
      expected_version: normalized.expectedVersion,
      site_id: normalized.siteId,
    }),
    signal,
  }));
}

export async function addSpaceMembersBatch(
  input: SpaceMemberBatchInput,
  signal?: AbortSignal,
): Promise<SpaceMemberBatchResult> {
  const normalized = assertSpaceMemberBatchInput(input);
  const target = normalized.target.mode === "existing"
    ? {
        mode: "existing" as const,
        space_id: normalized.target.spaceId,
        space_name: normalized.target.spaceName,
        expected_version: normalized.target.expectedVersion,
      }
    : {
        mode: "create" as const,
        space_name: normalized.target.spaceName,
      };
  return normalizeSpaceMemberBatchResult(await request("/member-batches", {
    method: "POST",
    body: JSON.stringify({
      target,
      site_ids: normalized.siteIds,
      operation_id: normalized.operationId,
    }),
    signal,
  }));
}

export async function reorderSpaceMembers(
  spaceId: string,
  input: SpaceReorderInput,
  signal?: AbortSignal,
): Promise<Space> {
  const normalized = assertSpaceReorderInput(input);
  return normalizeSpace(await request(`/${encodeId(spaceId)}/members/order`, {
    method: "PATCH",
    body: JSON.stringify({
      expected_version: normalized.expectedVersion,
      ordered_site_ids: normalized.orderedSiteIds,
      ...(normalized.beforeSiteId !== undefined
        ? { before_site_id: normalized.beforeSiteId }
        : {}),
    }),
    signal,
  }));
}

export async function removeSpaceMember(
  spaceId: string,
  siteId: string,
  expectedVersion: number,
  signal?: AbortSignal,
): Promise<SpaceMemberDeleteResult> {
  const params = new URLSearchParams({
    expected_version: String(assertSpaceExpectedVersion(expectedVersion)),
  });
  return normalizeSpaceMemberDeleteResult(
    await request(
      `/${encodeId(spaceId)}/members/${encodeId(siteId)}?${params.toString()}`,
      { method: "DELETE", signal },
    ),
  );
}
