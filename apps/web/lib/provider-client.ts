import {
  assertProviderExpectedVersion,
  assertProviderKind,
  normalizeProviderConfig,
  normalizeProviderConfigs,
  normalizeProviderConnectionTest,
  normalizeProviderRegistry,
  providerCreatePayload,
  providerErrorDetails,
  providerTestPayload,
  providerUpdatePayload,
  type ProviderConfig,
  type ProviderConnectionTest,
  type ProviderCreateInput,
  type ProviderKind,
  type ProviderRegistryItem,
  type ProviderTestInput,
  type ProviderUpdateInput,
} from "./provider-contract.ts";

// next.config.ts 把 /api/backend/* 重写到后端 /api/*，前端一律走这个前缀，
// 避免每处调用各自拼后端地址。
const PROVIDER_BASE = "/api/backend/providers";

export class ProviderApiError extends Error {
  status: number;
  code?: string;
  // 429 限流时来自 Retry-After 头，供 UI 提示「N 秒后可重试」。
  retryAfterSeconds?: number;

  constructor(status: number, message: string, code?: string, retryAfterSeconds?: number) {
    super(message);
    this.name = "ProviderApiError";
    this.status = status;
    if (code) this.code = code;
    if (retryAfterSeconds !== undefined) this.retryAfterSeconds = retryAfterSeconds;
  }
}

// Retry-After 头有两种合法形态：秒数或 HTTP 日期（RFC 9110），两种都要能解析，
// 否则换个网关实现前端就拿不到重试时间。
function parseRetryAfterSeconds(header: string | null): number | undefined {
  if (!header) return undefined;
  const candidate = header.trim();
  if (/^\d+$/.test(candidate)) {
    const seconds = Number(candidate);
    return Number.isSafeInteger(seconds) ? seconds : undefined;
  }
  const dateMs = Date.parse(candidate);
  if (!Number.isNaN(dateMs)) return Math.max(0, Math.ceil((dateMs - Date.now()) / 1000));
  return undefined;
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
  const response = await fetch(`${PROVIDER_BASE}${path}`, {
    ...init,
    // Provider 配置随时可能在别的标签页被改，读缓存会拿到过期的 version，必须直连。
    cache: "no-store",
    credentials: "include",
    // 只有带 body 的请求才声明 JSON，避免给 GET/DELETE 附加无意义的 Content-Type。
    headers: init.body
      ? { "Content-Type": "application/json", ...init.headers }
      : init.headers,
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const error = providerErrorDetails(response.status, payload);
    throw new ProviderApiError(
      response.status,
      error.message,
      error.code,
      response.status === 429 ? parseRetryAfterSeconds(response.headers.get("retry-after")) : undefined,
    );
  }
  return payload;
}

function encodeId(id: string): string {
  const normalized = id.trim();
  if (!normalized) throw new TypeError("Provider 配置 ID 不能为空");
  return encodeURIComponent(normalized);
}

export async function listProviderRegistry(signal?: AbortSignal): Promise<ProviderRegistryItem[]> {
  return normalizeProviderRegistry(await request("/registry", { signal }));
}

export async function listProviderConfigs(
  kind?: ProviderKind,
  signal?: AbortSignal,
): Promise<ProviderConfig[]> {
  const suffix =
    kind === undefined ? "" : `?${new URLSearchParams({ kind: assertProviderKind(kind) }).toString()}`;
  return normalizeProviderConfigs(await request(suffix, { signal }));
}

export async function createProviderConfig(input: ProviderCreateInput): Promise<ProviderConfig> {
  return normalizeProviderConfig(await request("", {
    method: "POST",
    body: JSON.stringify(providerCreatePayload(input)),
  }));
}

export async function updateProviderConfig(id: string, input: ProviderUpdateInput): Promise<ProviderConfig> {
  return normalizeProviderConfig(await request(`/${encodeId(id)}`, {
    method: "PATCH",
    body: JSON.stringify(providerUpdatePayload(input)),
  }));
}

export async function enableProviderConfig(id: string, expectedVersion: number): Promise<ProviderConfig> {
  return normalizeProviderConfig(await request(`/${encodeId(id)}/enable`, {
    method: "POST",
    body: JSON.stringify({ expected_version: assertProviderExpectedVersion(expectedVersion) }),
  }));
}

export async function deleteProviderConfig(id: string, expectedVersion: number): Promise<void> {
  // 后端契约里 DELETE 的 expected_version 走查询参数而不是请求体。
  const params = new URLSearchParams({
    expected_version: String(assertProviderExpectedVersion(expectedVersion)),
  });
  await request(`/${encodeId(id)}?${params.toString()}`, { method: "DELETE" });
}

export async function testProviderConnection(input: ProviderTestInput): Promise<ProviderConnectionTest> {
  // 先在本地构造请求体：非法组合（如 configId 却没有 expectedVersion）会在这里抛错，
  // 根本不会发出注定失败的请求。
  const payload = providerTestPayload(input);
  return normalizeProviderConnectionTest(await request("/test-connection", {
    method: "POST",
    body: JSON.stringify(payload),
  }));
}
