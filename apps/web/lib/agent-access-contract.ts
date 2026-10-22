import { createContractGuards } from "./contract-guards.ts";

/**
 * 外部 Agent 接入（Q43）：MCP 令牌、Agent 写入审计与配置片段。
 *
 * 令牌明文只出现在创建响应里一次；列表只有展示前缀，绝不含哈希。
 * 配置片段在这里生成（纯函数、可测），页面只负责展示与复制。
 */

export const AGENT_SCOPES = ["read", "write"] as const;
export type AgentScope = (typeof AGENT_SCOPES)[number];
export type AgentAccessLevel = "read" | "read-write";

export const AGENT_TOKEN_NAME_MAX = 40;
export const MCP_PROXY_PATH = "/api/backend/mcp";
export const TOKEN_PLACEHOLDER = "<令牌>";

export type AgentToken = {
  id: string;
  name: string;
  // 例如 whk_AbCd：只用于区分不同令牌。
  prefix: string;
  scopes: AgentScope[];
  canWrite: boolean;
  createdAt: string;
  lastUsedAt: string | null;
};

export type CreatedAgentToken = AgentToken & {
  // 完整令牌：只在创建成功的这一刻可见，关闭对话框后不再展示。
  token: string;
};

export type AgentTokenList = {
  items: AgentToken[];
  maxActive: number;
};

export type AgentActivity = {
  id: string;
  createdAt: string;
  tokenId: string;
  tokenName: string;
  tool: string;
  siteId: string | null;
  summary: string;
};

export type AgentTokenCreateInput = {
  name: string;
  access: AgentAccessLevel;
};

export type AgentAccessErrorDetails = {
  code?: string;
  message: string;
};

export class AgentAccessContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AgentAccessContractError";
  }
}

const guards = createContractGuards((message) => new AgentAccessContractError(message));
const { record, text, boundedText, identifier, count, literal, listPayload } = guards;

// SQLite 返回的时间不带时区偏移（存的是 UTC），统一补成带 Z 的 ISO，
// 否则 new Date() 会把它当本地时间，海外/跨时区显示会错位。
const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/iu;

export function utcTimestamp(value: unknown, path: string): string {
  const candidate = text(value, path);
  const normalized = HAS_ZONE.test(candidate) ? candidate : `${candidate}Z`;
  if (Number.isNaN(Date.parse(normalized))) {
    throw new AgentAccessContractError(`${path} 必须是有效时间`);
  }
  return normalized;
}

function nullableUtcTimestamp(value: unknown, path: string): string | null {
  if (value === null || value === undefined) return null;
  return utcTimestamp(value, path);
}

function scopes(value: unknown, path: string): AgentScope[] {
  if (!Array.isArray(value) || value.length === 0) {
    throw new AgentAccessContractError(`${path} 必须是非空数组`);
  }
  const selected = new Set(value.map((item, index) => literal(item, `${path}[${index}]`, AGENT_SCOPES)));
  // write 隐含 read：后端保证，这里再兜一次底，界面只按两档展示。
  if (selected.has("write")) selected.add("read");
  return AGENT_SCOPES.filter((scope) => selected.has(scope));
}

export function normalizeAgentToken(value: unknown, path = "token"): AgentToken {
  const candidate = record(value, path);
  const tokenScopes = scopes(candidate.scopes, `${path}.scopes`);
  return {
    id: identifier(candidate.id, `${path}.id`),
    name: boundedText(candidate.name, `${path}.name`, AGENT_TOKEN_NAME_MAX),
    prefix: text(candidate.prefix, `${path}.prefix`),
    scopes: tokenScopes,
    canWrite: tokenScopes.includes("write"),
    createdAt: utcTimestamp(candidate.created_at, `${path}.created_at`),
    lastUsedAt: nullableUtcTimestamp(candidate.last_used_at, `${path}.last_used_at`),
  };
}

export function normalizeCreatedAgentToken(value: unknown): CreatedAgentToken {
  const candidate = record(value, "token");
  const token = text(candidate.token, "token.token");
  if (!token.startsWith("whk_")) {
    throw new AgentAccessContractError("token.token 格式不正确");
  }
  return { ...normalizeAgentToken(candidate), token };
}

// 放进列表 state 的只是展示字段：明文令牌不随列表项留存。
export function listedAgentToken(token: AgentToken): AgentToken {
  return {
    id: token.id,
    name: token.name,
    prefix: token.prefix,
    scopes: [...token.scopes],
    canWrite: token.canWrite,
    createdAt: token.createdAt,
    lastUsedAt: token.lastUsedAt,
  };
}

export function normalizeAgentTokenList(value: unknown): AgentTokenList {
  const candidate = record(value, "tokens");
  return {
    items: listPayload(candidate, "tokens").map((item, index) =>
      normalizeAgentToken(item, `tokens.items[${index}]`),
    ),
    maxActive: count(candidate.max_active ?? 20, "tokens.max_active"),
  };
}

export function normalizeAgentActivityList(value: unknown): AgentActivity[] {
  return listPayload(value, "activity").map((item, index) => {
    const path = `activity.items[${index}]`;
    const candidate = record(item, path);
    const siteId = candidate.site_id;
    return {
      id: identifier(candidate.id, `${path}.id`),
      createdAt: utcTimestamp(candidate.created_at, `${path}.created_at`),
      tokenId: identifier(candidate.token_id, `${path}.token_id`),
      tokenName: text(candidate.token_name, `${path}.token_name`),
      tool: text(candidate.tool, `${path}.tool`),
      siteId: siteId === null || siteId === undefined ? null : identifier(siteId, `${path}.site_id`),
      summary: text(candidate.summary, `${path}.summary`),
    };
  });
}

export function agentTokenCreatePayload(input: AgentTokenCreateInput): {
  name: string;
  scopes: AgentScope[];
} {
  const name = input.name.normalize("NFKC").split(/\s+/u).filter(Boolean).join(" ");
  if (!name) throw new AgentAccessContractError("请填写令牌名称");
  if (Array.from(name).length > AGENT_TOKEN_NAME_MAX) {
    throw new AgentAccessContractError(`令牌名称不能超过 ${AGENT_TOKEN_NAME_MAX} 个字符`);
  }
  const access = literal(input.access, "access", ["read", "read-write"] as const);
  return { name, scopes: access === "read" ? ["read"] : ["read", "write"] };
}

export function agentAccessLabel(token: Pick<AgentToken, "canWrite">): string {
  return token.canWrite ? "读写" : "只读";
}

export function agentTokenDisplayPrefix(token: Pick<AgentToken, "prefix">): string {
  return `${token.prefix}…`;
}

const TOOL_LABELS: Record<string, string> = {
  save_resource: "收藏",
  update_resource: "整理",
  mark_resource: "标记",
};

export function agentToolLabel(tool: string): string {
  return TOOL_LABELS[tool] ?? tool;
}

export function agentErrorDetails(status: number, payload: unknown): AgentAccessErrorDetails {
  let code: string | undefined;
  let message: string | undefined;
  if (typeof payload === "object" && payload !== null && !Array.isArray(payload)) {
    const detail = (payload as Record<string, unknown>).detail;
    if (typeof detail === "string" && detail.trim()) {
      message = detail.trim();
    } else if (Array.isArray(detail)) {
      // FastAPI 422：字段校验错误是数组，取第一条的 msg。
      const first = detail.find((entry) => typeof entry === "object" && entry !== null) as
        | Record<string, unknown>
        | undefined;
      if (typeof first?.msg === "string" && first.msg.trim()) message = first.msg.trim();
    } else if (typeof detail === "object" && detail !== null) {
      const structured = detail as Record<string, unknown>;
      if (typeof structured.code === "string" && structured.code.trim()) code = structured.code.trim();
      if (typeof structured.message === "string" && structured.message.trim()) {
        message = structured.message.trim();
      }
    }
  }
  return {
    ...(code ? { code } : {}),
    message: message ?? fallbackMessage(status),
  };
}

function fallbackMessage(status: number): string {
  if (status === 401) return "登录已失效，请重新登录";
  if (status === 403) return "请求来源不受信任，请刷新页面后重试";
  if (status === 404) return "令牌不存在或已被吊销";
  if (status === 409) return "有效令牌数量已达上限";
  if (status === 422) return "请求内容不符合要求";
  if (status >= 500) return "服务暂时不可用，请稍后重试";
  return "请求失败，请稍后重试";
}

// ---------------------------------------------------------------- 配置片段

export function mcpEndpointUrl(origin: string): string {
  return `${origin.trim().replace(/\/+$/u, "")}${MCP_PROXY_PATH}`;
}

function yamlQuoted(value: string): string {
  return `"${value.replace(/\\/gu, "\\\\").replace(/"/gu, '\\"')}"`;
}

export type McpSnippetInput = {
  url: string;
  token?: string | null;
};

export function hermesConfigSnippet({ url, token }: McpSnippetInput): string {
  const bearer = `Bearer ${token || TOKEN_PLACEHOLDER}`;
  return [
    "mcp_servers:",
    "  webhub:",
    `    url: ${yamlQuoted(url)}`,
    "    headers:",
    `      Authorization: ${yamlQuoted(bearer)}`,
    "",
  ].join("\n");
}

export function genericMcpConfigSnippet({ url, token }: McpSnippetInput): string {
  return `${JSON.stringify(
    {
      mcpServers: {
        webhub: {
          type: "http",
          url,
          headers: { Authorization: `Bearer ${token || TOKEN_PLACEHOLDER}` },
        },
      },
    },
    null,
    2,
  )}\n`;
}

// ------------------------------------------------------------------ 时间展示

export function formatAgentMoment(value: string | null, locale = "zh-CN"): string {
  if (value === null) return "从未使用";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}
