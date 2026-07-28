const REQUEST_SOURCE = "webhub-web";
const REQUEST_TARGET = "webhub-browser-extension";
const RESPONSE_SOURCE = "webhub-browser-extension";
const RESPONSE_TARGET = "webhub-web";
const BRIDGE_VERSION = 1;
const PROBE_TIMEOUT_MS = 1_500;
const OPERATION_STORAGE_PREFIX = "webhub.space-group.operation.v1.";
const OPERATION_TTL_MS = 7 * 24 * 60 * 60 * 1000;
// The content-script bridge owns a 90 second timeout. Keep the page timeout
// slightly longer so the page cannot declare failure while the bridge still
// has a live request that may create a group.
const OPEN_TIMEOUT_MS = 95_000;

type BridgeErrorPayload = {
  code: string;
  message: string;
};

type BridgeResponse<T> = {
  source: typeof RESPONSE_SOURCE;
  target: typeof RESPONSE_TARGET;
  version: typeof BRIDGE_VERSION;
  type: "RESPONSE";
  requestId: string;
  ok: boolean;
  result?: T;
  error?: BridgeErrorPayload;
};

type BridgePingResult = {
  capabilities: string[];
  maxTabs: number;
};

export type BrowserSpaceGroupResult = {
  openedCount: number;
  groupId: number;
  replayed: boolean;
};

export type BrowserSpaceGroupOperationInput = {
  spaceId: string;
  spaceName: string;
  urls: readonly string[];
};

export type BrowserSpaceGroupInput = BrowserSpaceGroupOperationInput & {
  operationId: string;
  operationStartedAt: number;
  recovery: boolean;
};

export type BrowserSpaceGroupOperationReservation = {
  operationId: string;
  operationStartedAt: number;
  recovery: boolean;
  fingerprint: string;
  input: BrowserSpaceGroupOperationInput & { urls: string[] };
};

type StoredBrowserSpaceGroupOperation = {
  version: 1;
  fingerprint: string;
  operationId: string;
  operationStartedAt: number;
  spaceId: string;
  spaceName: string;
  urls: string[];
  expiresAt: number;
};

export class BrowserSpaceGroupError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "BrowserSpaceGroupError";
    this.code = code;
  }
}

export function normalizeBrowserSpaceGroupUrls(urls: readonly string[]): string[] {
  const normalized: string[] = [];
  const seen = new Set<string>();

  for (const rawUrl of urls) {
    let parsed: URL;
    try {
      parsed = new URL(rawUrl);
    } catch {
      throw new BrowserSpaceGroupError("INVALID_URLS", "网站列表中包含无效网址。");
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new BrowserSpaceGroupError("INVALID_URLS", "网站列表中包含不支持的网址协议。");
    }
    if (!seen.has(parsed.href)) {
      seen.add(parsed.href);
      normalized.push(parsed.href);
    }
  }

  return normalized;
}

function browserGroupErrorMessage(code: string): string {
  if (code === "EXTENSION_UNAVAILABLE" || code === "EXTENSION_TIMEOUT") {
    return "浏览器分组助手未响应，请确认扩展已启用并刷新 WebHub。";
  }
  if (code === "TOO_MANY_URLS") return "本次网站数量超过浏览器助手的安全上限。";
  if (code === "EXTENSION_BUSY") return "浏览器助手已有多项待处理任务，请稍后重试。";
  if (code === "BROWSER_LOCK_UNAVAILABLE") return "当前浏览器不支持安全的分组任务锁。";
  if (code === "OPERATION_STORAGE_FAILED") return "浏览器无法保存本次分组任务，请检查站点存储权限。";
  if (code === "IDEMPOTENCY_CONFLICT") return "本次打开任务标识冲突，请重新点击分组打开。";
  if (code === "TAB_CREATE_FAILED" || code === "TAB_NAVIGATE_FAILED") {
    return "浏览器未能打开全部网站，本轮已取消。";
  }
  if (code === "TAB_GROUP_FAILED" || code === "TAB_GROUP_UPDATE_FAILED") {
    return "浏览器未能建立并命名标签组，本轮已取消。";
  }
  if (code === "TAB_ACTIVATE_FAILED") return "标签组已回滚，请重新执行本次打开任务。";
  if (code === "STORAGE_FAILED") return "浏览器助手无法保存防重复状态，请检查扩展存储权限。";
  if (code === "ALIAS_STORAGE_FAILED") return "浏览器分组已执行，但合并任务回执保存失败，请重试确认。";
  if (code === "RECOVERY_FAILED") return "上次分组任务尚未清理完成，请再次重试。";
  if (code === "CROSS_SESSION_PENDING") {
    return "浏览器上次退出时分组仍在创建。为避免重复打开，已暂停自动恢复。";
  }
  if (code.startsWith("INVALID_")) return "浏览器助手拒绝了无效的分组请求。";
  return "浏览器分组操作失败，请重试。";
}

function requestId(): string {
  return window.crypto.randomUUID();
}

function operationStorageKey(spaceId: string): string {
  return `${OPERATION_STORAGE_PREFIX}${encodeURIComponent(spaceId)}`;
}

function removeStoredOperationValue(spaceId: string): void {
  try {
    window.localStorage.removeItem(operationStorageKey(spaceId));
  } catch {
    throw new BrowserSpaceGroupError(
      "OPERATION_STORAGE_FAILED",
      "浏览器无法清理 Space 分组任务状态。",
    );
  }
}

function readStoredOperation(spaceId: string): StoredBrowserSpaceGroupOperation | null {
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(operationStorageKey(spaceId));
  } catch {
    throw new BrowserSpaceGroupError(
      "OPERATION_STORAGE_FAILED",
      "浏览器无法读取 Space 分组任务状态。",
    );
  }
  if (!raw) return null;

  let value: Partial<StoredBrowserSpaceGroupOperation>;
  try {
    value = JSON.parse(raw) as Partial<StoredBrowserSpaceGroupOperation>;
  } catch {
    removeStoredOperationValue(spaceId);
    return null;
  }
  if (
    value.version !== 1 ||
    typeof value.fingerprint !== "string" ||
    !/^[a-f0-9]{64}$/.test(value.fingerprint) ||
    typeof value.operationId !== "string" ||
    value.operationId.length === 0 ||
    !Number.isSafeInteger(value.operationStartedAt) ||
    (value.operationStartedAt ?? 0) <= 0 ||
    value.spaceId !== spaceId ||
    typeof value.spaceName !== "string" ||
    value.spaceName.trim().length === 0 ||
    !Array.isArray(value.urls) ||
    value.urls.length === 0 ||
    !value.urls.every((url) => typeof url === "string") ||
    typeof value.expiresAt !== "number" ||
    !Number.isFinite(value.expiresAt) ||
    value.expiresAt !== (value.operationStartedAt ?? 0) + OPERATION_TTL_MS ||
    value.expiresAt <= Date.now()
  ) {
    removeStoredOperationValue(spaceId);
    return null;
  }
  let urls: string[];
  try {
    urls = normalizeBrowserSpaceGroupUrls(value.urls);
  } catch {
    removeStoredOperationValue(spaceId);
    return null;
  }
  if (urls.length !== value.urls.length) {
    removeStoredOperationValue(spaceId);
    return null;
  }
  return { ...(value as StoredBrowserSpaceGroupOperation), urls };
}

async function operationFingerprint(input: BrowserSpaceGroupOperationInput): Promise<string> {
  const canonical = JSON.stringify({
    spaceId: input.spaceId,
    spaceName: input.spaceName,
    urls: normalizeBrowserSpaceGroupUrls(input.urls),
  });
  const bytes = new TextEncoder().encode(canonical);
  const digest = await window.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(
    new Uint8Array(digest),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
}

function createBrowserSpaceGroupOperationId(spaceId: string): string {
  return `space:${spaceId}:${window.crypto.randomUUID()}`;
}

function operationLockName(spaceId: string): string {
  return `webhub:space-group:${spaceId}`;
}

function withBrowserSpaceGroupOperationLock<T>(
  spaceId: string,
  task: () => Promise<T>,
): Promise<T> {
  if (typeof navigator === "undefined" || !navigator.locks) {
    return Promise.reject(new BrowserSpaceGroupError(
      "BROWSER_LOCK_UNAVAILABLE",
      "当前浏览器不支持安全的 Space 分组任务锁。",
    ));
  }
  return navigator.locks.request(operationLockName(spaceId), { mode: "exclusive" }, task);
}

export async function reserveBrowserSpaceGroupOperation(
  input: BrowserSpaceGroupOperationInput,
): Promise<BrowserSpaceGroupOperationReservation> {
  return withBrowserSpaceGroupOperationLock(input.spaceId, async () => {
    const existing = readStoredOperation(input.spaceId);
    if (existing) {
      return {
        operationId: existing.operationId,
        operationStartedAt: existing.operationStartedAt,
        recovery: true,
        fingerprint: existing.fingerprint,
        input: {
          spaceId: existing.spaceId,
          spaceName: existing.spaceName,
          urls: existing.urls,
        },
      };
    }

    const urls = normalizeBrowserSpaceGroupUrls(input.urls);
    if (urls.length === 0) {
      throw new BrowserSpaceGroupError("INVALID_URLS", "这个 Space 还没有可打开的网站。");
    }
    const normalizedInput = {
      spaceId: input.spaceId,
      spaceName: input.spaceName,
      urls,
    };
    const fingerprint = await operationFingerprint(normalizedInput);
    const operationId = createBrowserSpaceGroupOperationId(input.spaceId);
    const operationStartedAt = Date.now();
    const record: StoredBrowserSpaceGroupOperation = {
      version: 1,
      fingerprint,
      operationId,
      operationStartedAt,
      ...normalizedInput,
      expiresAt: operationStartedAt + OPERATION_TTL_MS,
    };
    try {
      window.localStorage.setItem(operationStorageKey(input.spaceId), JSON.stringify(record));
    } catch {
      throw new BrowserSpaceGroupError(
        "OPERATION_STORAGE_FAILED",
        "浏览器无法保存本次 Space 分组任务。",
      );
    }
    return {
      operationId,
      operationStartedAt,
      recovery: false,
      fingerprint,
      input: normalizedInput,
    };
  });
}

export async function hasUnresolvedBrowserSpaceGroupOperation(
  spaceId: string,
): Promise<boolean> {
  return readStoredOperation(spaceId) !== null;
}

export function clearBrowserSpaceGroupOperation(
  spaceId: string,
  operationId: string,
): Promise<void> {
  return withBrowserSpaceGroupOperationLock(spaceId, async () => {
    const existing = readStoredOperation(spaceId);
    if (existing?.operationId !== operationId) return;
    removeStoredOperationValue(spaceId);
  });
}

function isBridgeResponse<T>(candidate: unknown, expectedRequestId: string): candidate is BridgeResponse<T> {
  if (!candidate || typeof candidate !== "object") return false;
  const value = candidate as Partial<BridgeResponse<T>>;
  return value.source === RESPONSE_SOURCE &&
    value.target === RESPONSE_TARGET &&
    value.version === BRIDGE_VERSION &&
    value.type === "RESPONSE" &&
    value.requestId === expectedRequestId &&
    typeof value.ok === "boolean";
}

function bridgeRequest<T>(
  type: "PING" | "OPEN_SPACE_GROUP",
  payload: Record<string, unknown>,
  timeoutMs: number,
): Promise<T> {
  if (typeof window === "undefined") {
    return Promise.reject(
      new BrowserSpaceGroupError("EXTENSION_UNAVAILABLE", "浏览器分组助手不可用。"),
    );
  }

  const id = requestId();
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      callback();
    };
    const onMessage = (event: MessageEvent<unknown>) => {
      if (event.source !== window || event.origin !== window.location.origin) return;
      if (!isBridgeResponse<T>(event.data, id)) return;
      if (event.data.ok && event.data.result !== undefined) {
        finish(() => resolve(event.data.result as T));
        return;
      }
      const error = event.data.error;
      const code = error?.code ?? "EXTENSION_ERROR";
      finish(() => reject(new BrowserSpaceGroupError(code, browserGroupErrorMessage(code))));
    };
    const timer = window.setTimeout(() => {
      finish(() => reject(new BrowserSpaceGroupError(
        type === "PING" ? "EXTENSION_UNAVAILABLE" : "EXTENSION_TIMEOUT",
        type === "PING" ? "浏览器分组助手未连接。" : "浏览器分组操作超时，请重试。",
      )));
    }, timeoutMs);

    window.addEventListener("message", onMessage);
    window.postMessage(
      {
        source: REQUEST_SOURCE,
        target: REQUEST_TARGET,
        version: BRIDGE_VERSION,
        type,
        requestId: id,
        payload,
      },
      window.location.origin,
    );
  });
}

export async function probeBrowserSpaceGroups(): Promise<BridgePingResult | null> {
  try {
    const result = await bridgeRequest<BridgePingResult>("PING", {}, PROBE_TIMEOUT_MS);
    if (!Array.isArray(result.capabilities) || !result.capabilities.includes("tabGroups")) {
      return null;
    }
    if (!Number.isInteger(result.maxTabs) || result.maxTabs < 1) return null;
    return result;
  } catch {
    return null;
  }
}

export function openSpaceInBrowserGroup(
  input: BrowserSpaceGroupInput,
): Promise<BrowserSpaceGroupResult> {
  const urls = normalizeBrowserSpaceGroupUrls(input.urls);
  return bridgeRequest<unknown>(
    "OPEN_SPACE_GROUP",
    {
      operationId: input.operationId,
      operationStartedAt: input.operationStartedAt,
      recovery: input.recovery,
      spaceId: input.spaceId,
      spaceName: input.spaceName,
      urls,
    },
    OPEN_TIMEOUT_MS,
  ).then((result) => {
    if (!result || typeof result !== "object") {
      throw new BrowserSpaceGroupError("INVALID_RESPONSE", "浏览器助手返回了无效结果。");
    }
    const value = result as Partial<BrowserSpaceGroupResult>;
    if (
      !Number.isInteger(value.openedCount) || (value.openedCount ?? 0) < 1 ||
      !Number.isInteger(value.groupId) || (value.groupId ?? -1) < 0 ||
      typeof value.replayed !== "boolean"
    ) {
      throw new BrowserSpaceGroupError("INVALID_RESPONSE", "浏览器助手返回了无效结果。");
    }
    return value as BrowserSpaceGroupResult;
  });
}
