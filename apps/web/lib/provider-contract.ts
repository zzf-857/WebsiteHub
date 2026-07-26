export const PROVIDER_KINDS = ["model", "search", "embedding"] as const;
export type ProviderKind = (typeof PROVIDER_KINDS)[number];

// 后端永远只回掩码，前端所有展示位统一引用这个常量，避免各处手写星号。
export const SECRET_MASK = "********";

export type ProviderRegistryItem = {
  provider: string;
  label: string;
  kinds: ProviderKind[];
  secretRequired: boolean;
  baseUrlRequired: boolean;
  allowsPrivateBaseUrl: boolean;
  applicationUrl: string | null;
  connectionTestSupported: boolean;
};

export type ProviderConfig = {
  id: string;
  kind: ProviderKind;
  provider: string;
  displayName: string;
  baseUrl: string | null;
  modelName: string | null;
  enabled: boolean;
  hasSecret: boolean;
  secretMask: string | null;
  version: number;
  createdAt: string;
  updatedAt: string;
};

export type ProviderCreateInput = {
  kind: ProviderKind;
  provider: string;
  displayName: string;
  baseUrl?: string | null;
  modelName?: string | null;
  // 明文密钥只在「提交这一刻」经过这里，构造完请求体后调用方必须立即清空来源 state。
  secret?: string;
  enabled?: boolean;
};

// 更新时对密钥只有三种意图：不动（undefined）、换新（replace）、清除（clear）。
// 用判别联合而不是裸字符串，逼调用方显式表达意图，避免「空字符串到底是清除还是保留」的歧义。
export type ProviderUpdateSecretInput =
  | { action: "replace"; value: string }
  | { action: "clear" };

export type ProviderUpdateInput = {
  expectedVersion: number;
  displayName?: string;
  baseUrl?: string | null;
  modelName?: string | null;
  secret?: ProviderUpdateSecretInput;
  enabled?: boolean;
};

// 后端对测试请求有互斥约束：要么测已保存配置（configId + expectedVersion），
// 要么测未保存参数（kind + provider + 可选 baseUrl/modelName/secret）。
// 这里故意用一个全可选的宽类型 + 运行时校验，让非法组合在前端就报错、不发请求。
export type ProviderTestInput = {
  configId?: string;
  expectedVersion?: number;
  kind?: ProviderKind;
  provider?: string;
  baseUrl?: string | null;
  modelName?: string | null;
  secret?: string;
};

export type ProviderConnectionTest = {
  status: string;
  code: string | null;
  message: string | null;
  kind: ProviderKind | null;
  provider: string | null;
};

export type ProviderErrorDetails = {
  code?: string;
  message: string;
};

type JsonRecord = Record<string, unknown>;

export class ProviderContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProviderContractError";
  }
}

function record(value: unknown, path: string): JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ProviderContractError(`${path} 必须是对象`);
  }
  return value as JsonRecord;
}

function text(value: unknown, path: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new ProviderContractError(`${path} 必须是非空字符串`);
  }
  return value.trim();
}

function nullableText(value: unknown, path: string): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") throw new ProviderContractError(`${path} 必须是字符串或 null`);
  return value.trim() || null;
}

function literal<const Values extends readonly string[]>(
  value: unknown,
  path: string,
  values: Values,
): Values[number] {
  if (typeof value !== "string" || !values.some((candidate) => candidate === value)) {
    throw new ProviderContractError(`${path} 不是受支持的值`);
  }
  return value as Values[number];
}

function identifier(value: unknown, path: string): string {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return String(value);
  throw new ProviderContractError(`${path} 必须是有效标识符`);
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw new ProviderContractError(`${path} 必须是布尔值`);
  return value;
}

function version(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) {
    throw new ProviderContractError(`${path} 必须是正整数`);
  }
  return value as number;
}

function absoluteWebUrl(value: unknown, path: string): string {
  const candidate = text(value, path);
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("protocol");
  } catch {
    throw new ProviderContractError(`${path} 必须是 HTTP(S) URL`);
  }
  return candidate;
}

function nullableWebUrl(value: unknown, path: string): string | null {
  if (value === null || value === undefined) return null;
  return absoluteWebUrl(value, path);
}

function isoDate(value: unknown, path: string): string {
  const candidate = text(value, path);
  if (Number.isNaN(Date.parse(candidate))) throw new ProviderContractError(`${path} 必须是有效日期`);
  return candidate;
}

// 密钥值只做最小处理：非空校验 + 去掉粘贴时带入的首尾空白（几乎必然是误粘贴的换行/空格）。
// 除此之外不做任何加工，更不允许把它塞进任何会被序列化或持久化的结构。
function secretValue(value: unknown, path: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new ProviderContractError(`${path} 必须是非空字符串`);
  }
  return value.trim();
}

export function assertProviderKind(value: unknown): ProviderKind {
  return literal(value, "provider.kind", PROVIDER_KINDS);
}

export function assertProviderExpectedVersion(value: unknown): number {
  return version(value, "provider.expected_version");
}

// 键名归一化后命中即视为明文密钥：覆盖 secret / api_key / apiKey / token 等常见拼法。
// 允许的 has_secret / secret_mask 归一化后分别是 hassecret / secretmask，不会误伤。
const PLAINTEXT_SECRET_KEYS = new Set([
  "secret",
  "secretvalue",
  "plaintextsecret",
  "apikey",
  "apisecret",
  "token",
  "apitoken",
  "accesstoken",
  "password",
  "credential",
  "credentials",
]);

// 后端契约规定密钥只以掩码（secret_mask）形式返回，绝不回明文。
// 如果响应里出现疑似明文密钥的键，说明后端存在严重缺陷；此时前端宁可抛错中断，
// 也绝不能把明文带进 state——一旦进入 state，就可能顺着日志、devtools、
// 错误上报等序列化路径泄露出去。
function assertNoPlaintextSecret(candidate: JsonRecord, path: string): void {
  for (const key of Object.keys(candidate)) {
    const normalized = key.toLowerCase().replace(/[_-]/g, "");
    if (PLAINTEXT_SECRET_KEYS.has(normalized)) {
      throw new ProviderContractError(`${path}.${key} 疑似包含明文密钥，已拒绝解析该响应`);
    }
  }
}

function normalizeRegistryItemAt(value: unknown, path: string): ProviderRegistryItem {
  const candidate = record(value, path);
  if (!Array.isArray(candidate.kinds) || candidate.kinds.length === 0) {
    throw new ProviderContractError(`${path}.kinds 必须是非空数组`);
  }
  return {
    provider: text(candidate.provider, `${path}.provider`),
    label: text(candidate.label, `${path}.label`),
    kinds: candidate.kinds.map((kind, index) => literal(kind, `${path}.kinds[${index}]`, PROVIDER_KINDS)),
    secretRequired: boolean(candidate.secret_required, `${path}.secret_required`),
    baseUrlRequired: boolean(candidate.base_url_required, `${path}.base_url_required`),
    allowsPrivateBaseUrl: boolean(candidate.allows_private_base_url, `${path}.allows_private_base_url`),
    applicationUrl: nullableWebUrl(candidate.application_url, `${path}.application_url`),
    connectionTestSupported: boolean(candidate.connection_test_supported, `${path}.connection_test_supported`),
  };
}

function listPayload(value: unknown, path: string): unknown[] {
  if (Array.isArray(value)) return value;
  const candidate = record(value, path);
  if (!Array.isArray(candidate.items)) throw new ProviderContractError(`${path}.items 必须是数组`);
  return candidate.items;
}

export function normalizeProviderRegistry(value: unknown): ProviderRegistryItem[] {
  return listPayload(value, "registry").map((item, index) => normalizeRegistryItemAt(item, `registry[${index}]`));
}

export function normalizeProviderConfig(value: unknown): ProviderConfig {
  const candidate = record(value, "provider_config");
  assertNoPlaintextSecret(candidate, "provider_config");

  return {
    id: identifier(candidate.id, "provider_config.id"),
    kind: literal(candidate.kind, "provider_config.kind", PROVIDER_KINDS),
    provider: text(candidate.provider, "provider_config.provider"),
    displayName: text(candidate.display_name, "provider_config.display_name"),
    baseUrl: nullableWebUrl(candidate.base_url, "provider_config.base_url"),
    modelName: nullableText(candidate.model_name, "provider_config.model_name"),
    enabled: boolean(candidate.enabled, "provider_config.enabled"),
    hasSecret: boolean(candidate.has_secret, "provider_config.has_secret"),
    secretMask: nullableText(candidate.secret_mask, "provider_config.secret_mask"),
    version: version(candidate.version, "provider_config.version"),
    createdAt: isoDate(candidate.created_at, "provider_config.created_at"),
    updatedAt: isoDate(candidate.updated_at, "provider_config.updated_at"),
  };
}

export function normalizeProviderConfigs(value: unknown): ProviderConfig[] {
  return listPayload(value, "provider_configs").map(normalizeProviderConfig);
}

export function normalizeProviderConnectionTest(value: unknown): ProviderConnectionTest {
  const candidate = record(value, "connection_test");
  return {
    status: text(candidate.status, "connection_test.status"),
    code: nullableText(candidate.code, "connection_test.code"),
    message: nullableText(candidate.message, "connection_test.message"),
    kind:
      candidate.kind === null || candidate.kind === undefined
        ? null
        : literal(candidate.kind, "connection_test.kind", PROVIDER_KINDS),
    provider: nullableText(candidate.provider, "connection_test.provider"),
  };
}

// 创建时密钥的 action 固定是 "write"（更新是 replace/clear，测试是 test，三者不可混用）。
export function providerCreatePayload(input: ProviderCreateInput): Record<string, unknown> {
  return {
    kind: literal(input.kind, "provider.kind", PROVIDER_KINDS),
    provider: text(input.provider, "provider.provider"),
    display_name: text(input.displayName, "provider.display_name"),
    ...(input.baseUrl !== undefined
      ? { base_url: input.baseUrl === null ? null : absoluteWebUrl(input.baseUrl, "provider.base_url") }
      : {}),
    ...(input.modelName !== undefined
      ? { model_name: input.modelName === null ? null : text(input.modelName, "provider.model_name") }
      : {}),
    ...(input.secret !== undefined
      ? { secret: { action: "write", value: secretValue(input.secret, "provider.secret") } }
      : {}),
    ...(input.enabled !== undefined ? { enabled: boolean(input.enabled, "provider.enabled") } : {}),
  };
}

// 更新的密钥语义由「是否携带 secret 键」承载：不携带 = 保留原密钥。
// 因此 secret 为 undefined 时必须完全省略这个键，输出 null/空对象都会被后端理解成别的意思。
export function providerUpdatePayload(input: ProviderUpdateInput): Record<string, unknown> {
  const expectedVersion = version(input.expectedVersion, "provider.expected_version");
  const hasEditableField =
    input.displayName !== undefined ||
    input.baseUrl !== undefined ||
    input.modelName !== undefined ||
    input.secret !== undefined ||
    input.enabled !== undefined;
  if (!hasEditableField) throw new ProviderContractError("Provider 更新至少需要一个字段");

  let secret: Record<string, unknown> | undefined;
  if (input.secret !== undefined) {
    const action = literal(input.secret.action, "provider.secret.action", ["replace", "clear"] as const);
    secret =
      action === "clear"
        ? { action: "clear" }
        : {
            action: "replace",
            value: secretValue((input.secret as { action: "replace"; value: string }).value, "provider.secret.value"),
          };
  }

  return {
    expected_version: expectedVersion,
    ...(input.displayName !== undefined
      ? { display_name: text(input.displayName, "provider.display_name") }
      : {}),
    ...(input.baseUrl !== undefined
      ? { base_url: input.baseUrl === null ? null : absoluteWebUrl(input.baseUrl, "provider.base_url") }
      : {}),
    ...(input.modelName !== undefined
      ? { model_name: input.modelName === null ? null : text(input.modelName, "provider.model_name") }
      : {}),
    ...(secret !== undefined ? { secret } : {}),
    ...(input.enabled !== undefined ? { enabled: boolean(input.enabled, "provider.enabled") } : {}),
  };
}

// 后端对测试请求的两种形态互斥，这里在前端提前拦截非法组合：
// 与其把必然 422 的请求发出去，不如在本地抛出更可读的错误。
export function providerTestPayload(input: ProviderTestInput): Record<string, unknown> {
  if (input.configId !== undefined) {
    if (
      input.kind !== undefined ||
      input.provider !== undefined ||
      input.baseUrl !== undefined ||
      input.modelName !== undefined ||
      input.secret !== undefined
    ) {
      throw new ProviderContractError("测试已保存配置时不能携带 kind/provider 等临时参数");
    }
    if (input.expectedVersion === undefined) {
      throw new ProviderContractError("测试已保存配置时必须提供 expectedVersion");
    }
    return {
      config_id: identifier(input.configId, "test.config_id"),
      expected_version: version(input.expectedVersion, "test.expected_version"),
    };
  }

  if (input.expectedVersion !== undefined) {
    throw new ProviderContractError("测试未保存配置时不能携带 expectedVersion");
  }
  if (input.kind === undefined || input.provider === undefined) {
    throw new ProviderContractError("测试未保存配置时必须提供 kind 与 provider");
  }
  return {
    kind: literal(input.kind, "test.kind", PROVIDER_KINDS),
    provider: text(input.provider, "test.provider"),
    ...(input.baseUrl !== undefined
      ? { base_url: input.baseUrl === null ? null : absoluteWebUrl(input.baseUrl, "test.base_url") }
      : {}),
    ...(input.modelName !== undefined
      ? { model_name: input.modelName === null ? null : text(input.modelName, "test.model_name") }
      : {}),
    ...(input.secret !== undefined
      ? { secret: { action: "test", value: secretValue(input.secret, "test.secret") } }
      : {}),
  };
}

function optionalErrorText(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function fallbackProviderErrorMessage(status: number): string {
  if (status === 401) return "登录状态已失效，请重新登录";
  if (status === 404) return "请求的 Provider 配置不存在";
  // 409 语义是乐观锁版本冲突：别人（或另一个标签页）已改过这条配置。
  if (status === 409) return "配置已被更新，请刷新后重试";
  if (status === 422) return "提交的信息不符合要求";
  if (status === 429) return "操作过于频繁，请稍后重试";
  return "Provider 服务暂时不可用，请稍后重试";
}

export function providerErrorDetails(status: number, payload: unknown): ProviderErrorDetails {
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

    // FastAPI 的 422 校验错误 detail 是数组，取第一条的 msg 作为兜底展示。
    if (Array.isArray(candidate.detail)) {
      const first = candidate.detail.find(
        (entry) => typeof entry === "object" && entry !== null && !Array.isArray(entry),
      ) as JsonRecord | undefined;
      message = optionalErrorText(first?.msg) ?? message;
    }
  }

  return {
    ...(code ? { code } : {}),
    message: message ?? fallbackProviderErrorMessage(status),
  };
}
