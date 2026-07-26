import type {
  ProviderConfig,
  ProviderCreateInput,
  ProviderKind,
  ProviderRegistryItem,
  ProviderUpdateInput,
} from "./provider-contract.ts";

// 表单的全部纯逻辑集中在这里：组件只负责渲染与调用，判断规则可以脱离 DOM 单测。
// 明文密钥在本模块里只作为函数参数「过一遍」，绝不被存进任何返回结构或模块级变量。

export type ProviderDraft = {
  provider: string;
  displayName: string;
  baseUrl: string;
  modelName: string;
  enabled: boolean;
};

// 编辑态对密钥只有三种意图，与后端的 secret action 一一对应：
// keep = 请求体里完全不出现 secret 键（保留原密钥）；write = replace；clear = clear。
// 新建态只可能是 keep（没填）或 write（填了）。
export type ProviderSecretIntent = "keep" | "write" | "clear";

export type ProviderDraftField =
  | "provider"
  | "displayName"
  | "baseUrl"
  | "modelName"
  | "secret";

export type ProviderDraftErrors = Partial<Record<ProviderDraftField, string>>;

export type ProviderKindSection = {
  kind: ProviderKind;
  title: string;
  description: string;
};

// 分区文案只描述**当前真的生效**的能力：向量服务的配置槽位后端还没有任何消费点
// （langgraph_runner 只解析 model 与 search），所以这里如实写明，不做能力预告。
export const PROVIDER_KIND_SECTIONS: readonly ProviderKindSection[] = [
  {
    kind: "model",
    title: "模型服务",
    description: "Agent 对话使用的大模型。同一时刻只有一个配置生效，启用新的会自动停用旧的。",
  },
  {
    kind: "search",
    title: "搜索服务",
    description: "Agent 联网搜索使用的服务。不配置时 Agent 只检索站内资料，不会访问外网。",
  },
  {
    kind: "embedding",
    title: "向量服务",
    description: "语义检索用的向量模型。检索链路尚未接入，此处保存的配置暂时不会被调用。",
  },
];

const KIND_LABELS: Record<ProviderKind, string> = {
  model: "模型服务",
  search: "搜索服务",
  embedding: "向量服务",
};

export function providerKindLabel(kind: ProviderKind): string {
  return KIND_LABELS[kind];
}

const MAX_DISPLAY_NAME_LENGTH = 80;
const MAX_MODEL_NAME_LENGTH = 160;
const MAX_BASE_URL_LENGTH = 2_048;

// 与后端 _display_name 的 " ".join(value.split()) 完全一致：折叠所有连续空白。
// 前后端用同一套归一化，才能让「有没有改动」的比对结果可信。
export function collapseDisplayName(value: string): string {
  return value.split(/\s+/u).filter(Boolean).join(" ");
}

const LOCAL_HOST_SUFFIXES = [".localhost", ".local", ".internal"];

// 私网/环回的本地预检：与 providers/targets.py 的规则对齐，但**只是提前给提示**。
// 真正的拦截永远在后端（它还会重新解析 DNS 以防重绑定），前端漏判不会造成安全问题。
function isLocalHostname(hostname: string): boolean {
  const host = hostname.replace(/\.$/u, "").toLowerCase();
  if (!host) return true;
  if (host === "localhost" || LOCAL_HOST_SUFFIXES.some((suffix) => host.endsWith(suffix))) {
    return true;
  }

  const bare = host.replace(/^\[/u, "").replace(/\]$/u, "");
  if (bare === "::1" || bare === "::") return true;

  const ipv4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/u.exec(bare);
  if (!ipv4) return false;
  const [first, second] = [Number(ipv4[1]), Number(ipv4[2])];
  if (first === 0 || first === 127 || first === 10) return true;
  if (first === 172 && second >= 16 && second <= 31) return true;
  if (first === 192 && second === 168) return true;
  if (first === 169 && second === 254) return true;
  return false;
}

function baseUrlError(value: string, allowsPrivate: boolean): string | undefined {
  if (value.length > MAX_BASE_URL_LENGTH) return "Base URL 过长";

  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return "Base URL 必须是完整的 HTTP(S) 地址，例如 https://api.example.com/v1";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return "Base URL 必须是完整的 HTTP(S) 地址，例如 https://api.example.com/v1";
  }
  if (parsed.username || parsed.password) return "Base URL 不能包含用户名或密码";
  if (parsed.search || parsed.hash) return "Base URL 不能包含查询参数或片段";
  if (!allowsPrivate && parsed.protocol !== "https:") {
    return "该服务商的 Base URL 必须使用 HTTPS";
  }
  if (!allowsPrivate && isLocalHostname(parsed.hostname)) {
    return "该服务商不允许指向本机或局域网地址";
  }
  return undefined;
}

export function createProviderDraft(provider = ""): ProviderDraft {
  // 新建默认勾选「保存后立即启用」：绝大多数人配完就是要用，
  // 少数只想先存不启用的场景可以自己取消勾选。
  return { provider, displayName: "", baseUrl: "", modelName: "", enabled: true };
}

export function editProviderDraft(config: ProviderConfig): ProviderDraft {
  return {
    provider: config.provider,
    displayName: config.displayName,
    baseUrl: config.baseUrl ?? "",
    modelName: config.modelName ?? "",
    enabled: config.enabled,
  };
}

export function effectiveHasSecret(
  intent: ProviderSecretIntent,
  hasStoredSecret: boolean,
): boolean {
  if (intent === "write") return true;
  if (intent === "clear") return false;
  return hasStoredSecret;
}

// "save" = 提交配置；"test" = 只跑一次连接测试。
// 两者要求的字段不同：测试是「列出该厂商有哪些模型」，要求先填模型名会让
// 最需要这份列表的人根本拉不到它——后端 test_connection 也同步放宽了这一条。
export type ProviderValidationMode = "save" | "test";

export type ProviderValidationInput = {
  kind: ProviderKind;
  definition: ProviderRegistryItem | null;
  draft: ProviderDraft;
  secretIntent: ProviderSecretIntent;
  hasStoredSecret: boolean;
  mode?: ProviderValidationMode;
};

// 完整性规则严格镜像后端 service._validate_complete：只有「启用」时才要求配齐，
// 未启用的半成品配置允许先存下来。前端提前拦是为了给中文字段级提示，
// 而不是为了替后端把关——同一条规则后端依然会独立执行一遍。
export function validateProviderDraft({
  kind,
  definition,
  draft,
  secretIntent,
  hasStoredSecret,
  mode = "save",
}: ProviderValidationInput): ProviderDraftErrors {
  if (definition === null) return { provider: "请先选择服务商" };

  const errors: ProviderDraftErrors = {};
  const testing = mode === "test";

  if (!testing) {
    const displayName = collapseDisplayName(draft.displayName);
    if (!displayName) {
      errors.displayName = "请填写配置名称";
    } else if (Array.from(displayName).length > MAX_DISPLAY_NAME_LENGTH) {
      errors.displayName = `配置名称不能超过 ${MAX_DISPLAY_NAME_LENGTH} 个字符`;
    }
  }

  const baseUrl = draft.baseUrl.trim();
  if (baseUrl) {
    const message = baseUrlError(baseUrl, definition.allowsPrivateBaseUrl);
    if (message) errors.baseUrl = message;
  } else if (definition.baseUrlRequired) {
    errors.baseUrl = "该服务商必须填写 Base URL";
  }

  const modelName = draft.modelName.trim();
  if (kind === "search") {
    if (modelName && !testing) errors.modelName = "搜索服务不需要填写模型名称";
  } else if (Array.from(modelName).length > MAX_MODEL_NAME_LENGTH) {
    errors.modelName = `模型名称不能超过 ${MAX_MODEL_NAME_LENGTH} 个字符`;
  } else if (!modelName && draft.enabled && !testing) {
    errors.modelName = "启用前必须填写模型名称";
  }

  if (definition.secretRequired) {
    if (testing) {
      // 测试要真的把 Key 发给厂商，没有 Key 就无从测起。
      if (!effectiveHasSecret(secretIntent, hasStoredSecret)) {
        errors.secret = "测试连接前必须填写 API Key";
      }
    } else if (secretIntent === "clear" && draft.enabled) {
      // 后端在这种组合下直接 422，提前说清楚比让它报错更友好。
      errors.secret = "清除 API Key 时不能同时启用该配置";
    } else if (draft.enabled && !effectiveHasSecret(secretIntent, hasStoredSecret)) {
      errors.secret = "启用前必须填写 API Key";
    }
  }

  return errors;
}

export function hasProviderDraftError(errors: ProviderDraftErrors): boolean {
  return Object.values(errors).some(Boolean);
}

export function buildProviderCreateInput({
  kind,
  draft,
  secret,
}: {
  kind: ProviderKind;
  draft: ProviderDraft;
  secret: string;
}): ProviderCreateInput {
  const baseUrl = draft.baseUrl.trim();
  const modelName = draft.modelName.trim();
  const trimmedSecret = secret.trim();
  return {
    kind,
    provider: draft.provider,
    displayName: collapseDisplayName(draft.displayName),
    baseUrl: baseUrl || null,
    modelName: modelName || null,
    // 没填密钥就整个不传 secret 键：Ollama 这类不需要 Key 的厂商靠这条留空提交。
    ...(trimmedSecret ? { secret: trimmedSecret } : {}),
    enabled: draft.enabled,
  };
}

// 只提交真正变化的字段。返回 null 表示「没有任何改动」，
// 调用方据此避免发出一个必然被后端判为空更新（422）的请求。
export function buildProviderUpdateInput({
  config,
  draft,
  secretIntent,
  secret,
}: {
  config: ProviderConfig;
  draft: ProviderDraft;
  secretIntent: ProviderSecretIntent;
  secret: string;
}): ProviderUpdateInput | null {
  const input: ProviderUpdateInput = { expectedVersion: config.version };
  let changed = false;

  const displayName = collapseDisplayName(draft.displayName);
  if (displayName !== config.displayName) {
    input.displayName = displayName;
    changed = true;
  }

  const baseUrl = draft.baseUrl.trim() || null;
  if (baseUrl !== config.baseUrl) {
    input.baseUrl = baseUrl;
    changed = true;
  }

  const modelName = draft.modelName.trim() || null;
  if (modelName !== config.modelName) {
    input.modelName = modelName;
    changed = true;
  }

  if (secretIntent === "write") {
    const value = secret.trim();
    // 意图是换新但一个字符都没填，等同于没动过——绝不能退化成 clear 把原密钥抹掉。
    if (value) {
      input.secret = { action: "replace", value };
      changed = true;
    }
  } else if (secretIntent === "clear") {
    input.secret = { action: "clear" };
    changed = true;
  }

  if (draft.enabled !== config.enabled) {
    input.enabled = draft.enabled;
    changed = true;
  }

  return changed ? input : null;
}

const FIELD_ERROR_CODES: Record<string, { field: ProviderDraftField; message: string }> = {
  duplicate_provider_name: {
    field: "displayName",
    message: "同类型下已存在同名配置，请换一个名称",
  },
  unsupported_provider: {
    field: "provider",
    message: "该服务商不支持这种配置类型",
  },
  invalid_base_url: { field: "baseUrl", message: "Base URL 无效" },
  insecure_base_url: {
    field: "baseUrl",
    message: "该服务商的 Base URL 必须使用 HTTPS",
  },
  unsafe_provider_target: {
    field: "baseUrl",
    message: "Base URL 指向了不允许访问的地址",
  },
  provider_target_timeout: { field: "baseUrl", message: "Base URL 的地址解析超时" },
  provider_target_unreachable: { field: "baseUrl", message: "Base URL 的地址无法解析" },
  invalid_secret_action: {
    field: "secret",
    message: "密钥操作无效，请重新填写后再试",
  },
};

// 把后端 error code 落到具体字段上，避免所有错误都堆在表单顶部。
// 认不出的 code 返回 null，由调用方展示表单级错误（用后端给的中文原文）。
export function providerFieldErrorFor(
  code: string | undefined,
  message: string,
): { field: ProviderDraftField; message: string } | null {
  if (!code) return null;
  const mapped = FIELD_ERROR_CODES[code];
  if (!mapped) return null;
  // 后端文案更具体时优先用后端的（例如 base_url 的多种失败原因）。
  return { field: mapped.field, message: message.trim() || mapped.message };
}

export type ProviderTestTone = "ok" | "error" | "rate-limited" | "neutral";

export function providerTestTone(status: string): ProviderTestTone {
  if (status === "ok" || status === "success") return "ok";
  if (status === "rate_limited" || status === "rate-limited") return "rate-limited";
  if (status === "error" || status === "failed") return "error";
  return "neutral";
}
