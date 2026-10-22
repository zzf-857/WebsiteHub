import {
  agentErrorDetails,
  agentTokenCreatePayload,
  normalizeAgentActivityList,
  normalizeAgentTokenList,
  normalizeCreatedAgentToken,
  type AgentActivity,
  type AgentTokenCreateInput,
  type AgentTokenList,
  type CreatedAgentToken,
} from "./agent-access-contract.ts";

// next.config.ts 把 /api/backend/* 重写到后端 /api/*。
const BACKEND = "/api/backend";
export const MAX_ACTIVITY_LIMIT = 200;

export class AgentAccessApiError extends Error {
  status: number;
  code?: string;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = "AgentAccessApiError";
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
  const response = await fetch(`${BACKEND}${path}`, {
    ...init,
    // 最近使用时间与活动记录随 Agent 调用实时变化，不能读缓存。
    cache: "no-store",
    credentials: "include",
    headers: init.body ? { "Content-Type": "application/json", ...init.headers } : init.headers,
  });
  const payload = await readJson(response);
  if (!response.ok) {
    const error = agentErrorDetails(response.status, payload);
    throw new AgentAccessApiError(response.status, error.message, error.code);
  }
  return payload;
}

function encodeId(id: string): string {
  const normalized = id.trim();
  if (!normalized) throw new TypeError("令牌 ID 不能为空");
  return encodeURIComponent(normalized);
}

export async function listAgentTokens(signal?: AbortSignal): Promise<AgentTokenList> {
  return normalizeAgentTokenList(await request("/agent-tokens", { signal }));
}

export async function createAgentToken(input: AgentTokenCreateInput): Promise<CreatedAgentToken> {
  // 先在本地校验并构造请求体：空名称或超长不会发出注定失败的请求。
  const payload = agentTokenCreatePayload(input);
  return normalizeCreatedAgentToken(
    await request("/agent-tokens", { method: "POST", body: JSON.stringify(payload) }),
  );
}

export async function revokeAgentToken(id: string): Promise<void> {
  await request(`/agent-tokens/${encodeId(id)}`, { method: "DELETE" });
}

export async function listAgentActivity(
  limit = 50,
  signal?: AbortSignal,
): Promise<AgentActivity[]> {
  const bounded = Math.min(MAX_ACTIVITY_LIMIT, Math.max(1, Math.trunc(limit)));
  const params = new URLSearchParams({ limit: String(bounded) });
  return normalizeAgentActivityList(await request(`/agent-activity?${params.toString()}`, { signal }));
}
