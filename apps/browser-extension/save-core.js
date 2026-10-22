// 「收藏当前页」的纯逻辑：设置校验、MCP JSON-RPC 消息构造、响应解析与反馈文案。
// 不碰任何 chrome.* API，Service Worker、选项页与 Node 单元测试共用同一份
// （apps/web/tests/browser-extension-save.test.mjs）。
(() => {
  "use strict";

  const SETTINGS_KEY = "webhub.save.settings.v1";
  const MCP_PATH = "/api/backend/mcp";
  const MCP_ACCEPT = "application/json, text/event-stream";
  const MAX_ORIGIN_INPUT_LENGTH = 2048;
  const MAX_URL_LENGTH = 4096;
  const MAX_TITLE_LENGTH = 1000;
  const MAX_DETAIL_LENGTH = 240;
  const REQUEST_TIMEOUT_MS = 20_000;
  const TOKEN_PATTERN = /^whk_[A-Za-z0-9_-]{16,250}$/;
  const RESOURCE_ID_PATTERN = /^[A-Za-z0-9-]{1,64}$/;
  const SCHEME_PATTERN = /^[a-z][a-z0-9+.-]*:\/\//i;
  const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]+/g;
  const NOTIFICATION_PREFIX = "webhub-save";
  const READ_ONLY_PREFIX = "this token is read-only";
  const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

  const ERROR_MESSAGES = Object.freeze({
    NOT_CONFIGURED: "还没有设置 WebHub 地址和令牌，请在扩展选项页填写。",
    UNSUPPORTED_URL: "只能收藏 http / https 网页。",
    PERMISSION_MISSING: "扩展还没有获得访问 WebHub 地址的授权，请在选项页点一次「保存」。",
    UNAUTHORIZED: "令牌无效或已吊销，请在 WebHub「设置 → Agent 接入」重新创建读写令牌。",
    READ_ONLY: "这是只读令牌，收藏需要读写令牌。",
    HOST_REJECTED: "WebHub 不接受这个主机名，请把它加入服务器的 WEBHUB_ALLOWED_HOSTS。",
    NOT_WEBHUB: "这个地址上找不到 WebHub 的 MCP 接口，请检查地址。",
    REDIRECTED: "这个地址发生了跳转，请填写最终地址（例如 https://…）。",
    UNAVAILABLE: "WebHub 后端暂时不可用，请稍后再试。",
    HTTP_ERROR: "WebHub 返回了意外的状态码。",
    NETWORK_ERROR: "连不上 WebHub，请检查地址与网络（例如 Tailscale 是否在线）。",
    TIMEOUT: "WebHub 响应超时，请稍后再试。",
    BAD_RESPONSE: "WebHub 返回了无法识别的内容。",
    RPC_ERROR: "WebHub 拒绝了这次请求。",
    TOOL_ERROR: "WebHub 没能完成这次收藏。",
  });

  // 需要回到选项页才能修好的错误：通知按钮给「打开设置」。
  const SETTINGS_ERRORS = new Set([
    "NOT_CONFIGURED",
    "PERMISSION_MISSING",
    "UNAUTHORIZED",
    "READ_ONLY",
    "HOST_REJECTED",
    "NOT_WEBHUB",
    "REDIRECTED",
  ]);

  // 取自 apps/web/app/globals.css 浅色主题令牌（扩展无法读取 CSS 变量）。
  const BADGES = Object.freeze({
    pending: Object.freeze({ text: "…", color: "#5e666e", textColor: "#ffffff" }),
    saved: Object.freeze({ text: "✓", color: "#4f7a0b", textColor: "#ffffff" }),
    already: Object.freeze({ text: "=", color: "#5e666e", textColor: "#ffffff" }),
    error: Object.freeze({ text: "!", color: "#c64c44", textColor: "#ffffff" }),
  });

  // 只看形状、不比较原型：存储与消息里的值可能经过结构化克隆。
  function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function singleLine(value, maximumLength) {
    if (typeof value !== "string") return "";
    const text = value.replace(CONTROL_CHARACTERS, " ").replace(/\s+/g, " ").trim();
    const characters = Array.from(text);
    if (characters.length <= maximumLength) return text;
    return `${characters.slice(0, maximumLength - 1).join("").trimEnd()}…`;
  }

  function hostOf(value) {
    try {
      return new URL(value).host;
    } catch {
      return "";
    }
  }

  // 接受 “https://x.ts.net”、“x.ts.net”、“localhost:3100”，以及从设置页整段复制来的
  // MCP 地址；只保留 origin。没写协议时回环地址补 http，其余补 https。
  function normalizeOrigin(value) {
    if (typeof value !== "string") return null;
    let text = value.trim();
    if (!text || text.length > MAX_ORIGIN_INPUT_LENGTH || /\s/.test(text)) return null;
    if (!SCHEME_PATTERN.test(text)) {
      const host = text.split(/[/?#]/, 1)[0].toLowerCase().replace(/:\d+$/, "");
      text = `${LOOPBACK_HOSTS.has(host) ? "http" : "https"}://${text}`;
    }
    let url;
    try {
      url = new URL(text);
    } catch {
      return null;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (url.username || url.password || !url.hostname) return null;
    return url.origin;
  }

  function isLoopbackOrigin(origin) {
    try {
      return LOOPBACK_HOSTS.has(new URL(origin).hostname);
    } catch {
      return false;
    }
  }

  // 明文 HTTP 且不是本机：令牌会不加密地经过网络，选项页据此提示。
  function isInsecureRemoteOrigin(origin) {
    return typeof origin === "string" && origin.startsWith("http:") && !isLoopbackOrigin(origin);
  }

  // Chrome 匹配模式不写端口即匹配该主机的任意端口；不带端口最稳妥。
  function permissionPattern(origin) {
    const url = new URL(origin);
    return `${url.protocol}//${url.hostname}/*`;
  }

  function normalizeToken(value) {
    if (typeof value !== "string") return null;
    const token = value.trim().replace(/^Bearer\s+/i, "");
    return TOKEN_PATTERN.test(token) ? token : null;
  }

  // 选项页展示用：与网站令牌列表一致，只露前缀。
  function tokenPreview(token) {
    return typeof token === "string" && token.length > 8 ? `${token.slice(0, 8)}…` : "";
  }

  function normalizeSettings(value) {
    if (!isRecord(value)) return null;
    const origin = normalizeOrigin(value.origin);
    const token = normalizeToken(value.token);
    if (!origin || !token || origin !== value.origin) return null;
    return { origin, token };
  }

  function mcpEndpoint(origin) {
    return `${origin}${MCP_PATH}`;
  }

  function agentsSettingsUrl(origin) {
    return `${origin}/settings/agents`;
  }

  function detailUrl(origin, resourceId) {
    if (typeof resourceId !== "string" || !RESOURCE_ID_PATTERN.test(resourceId)) return null;
    return `${origin}/library/${encodeURIComponent(resourceId)}`;
  }

  // 把标签页或链接变成 save_resource 的参数；不是 http(s)、含账号密码或过长时返回 null。
  function buildSaveArguments(target) {
    if (!isRecord(target) || typeof target.url !== "string") return null;
    let url;
    try {
      url = new URL(target.url.trim());
    } catch {
      return null;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (url.username || url.password || url.href.length > MAX_URL_LENGTH) return null;
    const args = { url: url.href };
    const title = singleLine(target.title, MAX_TITLE_LENGTH);
    if (title && title !== url.href && title !== target.url.trim()) args.title = title;
    return args;
  }

  function buildToolCallRequest(requestId, name, args) {
    return {
      jsonrpc: "2.0",
      id: requestId,
      method: "tools/call",
      params: { name, arguments: isRecord(args) ? args : {} },
    };
  }

  function buildRequestInit(token, body) {
    return {
      method: "POST",
      headers: {
        Accept: MCP_ACCEPT,
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
      // MCP 只认令牌：不带 Cookie；跳转一律视为配置错误，令牌不跟随到别处。
      credentials: "omit",
      cache: "no-store",
      redirect: "manual",
    };
  }

  function failure(code, detail) {
    const outcome = { ok: false, code, message: ERROR_MESSAGES[code] ?? ERROR_MESSAGES.HTTP_ERROR };
    const text = singleLine(detail, MAX_DETAIL_LENGTH);
    if (text) outcome.detail = text;
    return outcome;
  }

  function parseJson(text) {
    try {
      return JSON.parse(text);
    } catch {
      return undefined;
    }
  }

  // 服务端是 JSON 模式；万一回成事件流，取最后一条 data: 里的 JSON-RPC 消息。
  function parseRpcBody(text) {
    if (typeof text !== "string" || !text.trim()) return null;
    const direct = parseJson(text);
    if (isRecord(direct)) return direct;
    let message = null;
    for (const line of text.split(/\r?\n/)) {
      if (!line.startsWith("data:")) continue;
      const candidate = parseJson(line.slice(5).trim());
      if (isRecord(candidate) && candidate.jsonrpc === "2.0") message = candidate;
    }
    return message;
  }

  function firstText(result) {
    if (!Array.isArray(result.content)) return null;
    const item = result.content.find(
      (entry) => isRecord(entry) && entry.type === "text" && typeof entry.text === "string",
    );
    return item ? item.text : null;
  }

  function interpretToolResponse(response) {
    const status = isRecord(response) && Number.isInteger(response.status) ? response.status : 0;
    const type = isRecord(response) ? response.type : undefined;
    const bodyText = isRecord(response) && typeof response.bodyText === "string"
      ? response.bodyText
      : "";
    if (type === "opaqueredirect" || (status >= 300 && status < 400)) return failure("REDIRECTED");
    if (status === 401) return failure("UNAUTHORIZED");
    if (status === 421) return failure("HOST_REJECTED");
    if (status === 404 || status === 405) return failure("NOT_WEBHUB");
    if (status === 502 || status === 503 || status === 504) return failure("UNAVAILABLE");

    const message = parseRpcBody(bodyText);
    if (status < 200 || status >= 300) {
      if (status === 400 && message && isRecord(message.error)) {
        return failure("RPC_ERROR", message.error.message);
      }
      return failure("HTTP_ERROR", `HTTP ${status}`);
    }
    if (!message) return failure("BAD_RESPONSE");
    if (isRecord(message.error)) return failure("RPC_ERROR", message.error.message);
    if (!isRecord(message.result)) return failure("BAD_RESPONSE");

    const text = firstText(message.result);
    if (message.result.isError === true) {
      if (typeof text === "string" && text.trim().toLowerCase().startsWith(READ_ONLY_PREFIX)) {
        return failure("READ_ONLY");
      }
      return failure("TOOL_ERROR", text ?? "");
    }
    const payload = text === null ? undefined : parseJson(text);
    if (!isRecord(payload)) return failure("BAD_RESPONSE");
    return { ok: true, payload };
  }

  // 依赖注入 fetch，便于测试；所有异常都折叠成带中文说明的结果，不向外抛。
  async function callTool(options) {
    const { fetchImpl, origin, token, name } = options;
    const body = buildToolCallRequest(options.requestId ?? 1, name, options.arguments);
    const timeoutMs = options.timeoutMs ?? REQUEST_TIMEOUT_MS;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    let timedOut = false;
    const timer = controller
      ? setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, timeoutMs)
      : null;
    try {
      let response;
      try {
        response = await fetchImpl(mcpEndpoint(origin), {
          ...buildRequestInit(token, body),
          ...(controller ? { signal: controller.signal } : {}),
        });
      } catch {
        return failure(timedOut ? "TIMEOUT" : "NETWORK_ERROR");
      }
      let bodyText = "";
      if (response.type !== "opaqueredirect") {
        try {
          bodyText = await response.text();
        } catch {
          return failure(timedOut ? "TIMEOUT" : "NETWORK_ERROR");
        }
      }
      return interpretToolResponse({ status: response.status, type: response.type, bodyText });
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  // save_resource 的 {already_saved, resource} → 收藏结果；结构不对算无法识别。
  function interpretSave(outcome) {
    if (!isRecord(outcome) || outcome.ok !== true) {
      return isRecord(outcome) && outcome.ok === false ? outcome : failure("BAD_RESPONSE");
    }
    const { payload } = outcome;
    const resource = isRecord(payload.resource) ? payload.resource : null;
    if (typeof payload.already_saved !== "boolean" || !resource) return failure("BAD_RESPONSE");
    const id = typeof resource.id === "string" && RESOURCE_ID_PATTERN.test(resource.id)
      ? resource.id
      : null;
    if (!id) return failure("BAD_RESPONSE");
    return {
      ok: true,
      state: payload.already_saved ? "already" : "saved",
      resource: {
        id,
        title: singleLine(resource.title, 160),
        url: typeof resource.url === "string" ? resource.url : "",
      },
    };
  }

  function badgeFor(state) {
    return BADGES[state] ?? BADGES.error;
  }

  function encodeNotificationId(target, nonce) {
    const suffix = String(nonce ?? Date.now()).replace(/[^A-Za-z0-9-]/g, "");
    if (target && target.kind === "detail" && RESOURCE_ID_PATTERN.test(target.resourceId)) {
      return `${NOTIFICATION_PREFIX}|detail|${target.resourceId}|${suffix}`;
    }
    if (target && target.kind === "options") return `${NOTIFICATION_PREFIX}|options|${suffix}`;
    return `${NOTIFICATION_PREFIX}|none|${suffix}`;
  }

  function decodeNotificationId(value) {
    if (typeof value !== "string") return null;
    const parts = value.split("|");
    if (parts[0] !== NOTIFICATION_PREFIX) return null;
    if (parts[1] === "detail" && parts.length === 4 && RESOURCE_ID_PATTERN.test(parts[2])) {
      return { kind: "detail", resourceId: parts[2] };
    }
    if (parts[1] === "options" && parts.length === 3) return { kind: "options" };
    return null;
  }

  // 收藏结果 → 徽标状态 + 系统通知内容（含按钮去向）。
  function saveFeedback(result, context) {
    const pageTitle = singleLine(context && context.title, 160);
    const pageUrl = context && typeof context.url === "string" ? context.url : "";
    const nonce = context ? context.nonce : undefined;
    if (result && result.ok === true) {
      const label = result.resource.title || pageTitle || result.resource.url || pageUrl;
      return {
        state: result.state,
        notificationId: encodeNotificationId(
          { kind: "detail", resourceId: result.resource.id },
          nonce,
        ),
        title: result.state === "already" ? "之前已经收藏过" : "已收藏到 WebHub",
        message: label || "（无标题）",
        contextMessage: hostOf(result.resource.url || pageUrl),
        buttonTitle: "打开详情",
      };
    }
    const code = result && typeof result.code === "string" ? result.code : "BAD_RESPONSE";
    const message = (result && result.message) || ERROR_MESSAGES.BAD_RESPONSE;
    const toSettings = SETTINGS_ERRORS.has(code);
    return {
      state: "error",
      code,
      notificationId: encodeNotificationId(toSettings ? { kind: "options" } : null, nonce),
      title: "收藏失败",
      message: result && result.detail ? `${message}（${result.detail}）` : message,
      contextMessage: pageTitle || hostOf(pageUrl),
      buttonTitle: toSettings ? "打开设置" : null,
    };
  }

  // 通知被点击后去哪：详情页需要当时的 WebHub 地址；地址失效则不打开任何页面。
  function notificationDestination(notificationId, settings) {
    const target = decodeNotificationId(notificationId);
    if (!target) return null;
    if (target.kind === "options") return { kind: "options" };
    const normalized = normalizeSettings(settings);
    const url = normalized ? detailUrl(normalized.origin, target.resourceId) : null;
    return url ? { kind: "url", url } : null;
  }

  // 一次完整收藏：设置 → 网址 → 授权 → save_resource → 反馈。chrome.* 能力全部注入，
  // 便于在 Node 中测试整条流程；任何失败都折叠成反馈，不向外抛。
  async function performSave(options) {
    const target = isRecord(options) && isRecord(options.target) ? options.target : {};
    const context = {
      title: target.title,
      url: typeof target.url === "string" ? target.url : "",
      nonce: options && options.nonce,
    };
    const settings = normalizeSettings(options && options.settings);
    if (!settings) return saveFeedback(failure("NOT_CONFIGURED"), context);
    const args = buildSaveArguments(target);
    if (!args) return saveFeedback(failure("UNSUPPORTED_URL"), context);
    let permitted = false;
    try {
      permitted = (await options.hasPermission(permissionPattern(settings.origin))) === true;
    } catch {
      permitted = false;
    }
    if (!permitted) return saveFeedback(failure("PERMISSION_MISSING"), context);
    if (typeof options.onRequest === "function") {
      try {
        options.onRequest(args);
      } catch {
        // 徽标之类的旁路反馈失败不影响收藏本身。
      }
    }
    const outcome = await callTool({
      fetchImpl: options.fetchImpl,
      origin: settings.origin,
      token: settings.token,
      name: "save_resource",
      arguments: args,
      requestId: options.requestId,
      timeoutMs: options.timeoutMs,
    });
    return saveFeedback(interpretSave(outcome), context);
  }

  // 「测试连接」：list_taxonomy 的结果 → 一句中文。
  function describeConnection(outcome) {
    if (!isRecord(outcome) || outcome.ok !== true) {
      const message = (isRecord(outcome) && outcome.message) || ERROR_MESSAGES.BAD_RESPONSE;
      return {
        ok: false,
        text: isRecord(outcome) && outcome.detail ? `${message}（${outcome.detail}）` : message,
      };
    }
    const { payload } = outcome;
    if (!Array.isArray(payload.categories) || !Array.isArray(payload.tags)) {
      return { ok: false, text: ERROR_MESSAGES.BAD_RESPONSE };
    }
    const tagCount = Number.isInteger(payload.tag_total) ? payload.tag_total : payload.tags.length;
    return {
      ok: true,
      text: `连接成功：${payload.categories.length} 个分类、${tagCount} 个标签。`,
    };
  }

  const api = Object.freeze({
    SETTINGS_KEY,
    MCP_PATH,
    MCP_ACCEPT,
    MAX_URL_LENGTH,
    MAX_TITLE_LENGTH,
    REQUEST_TIMEOUT_MS,
    ERROR_MESSAGES,
    agentsSettingsUrl,
    badgeFor,
    buildRequestInit,
    buildSaveArguments,
    buildToolCallRequest,
    callTool,
    decodeNotificationId,
    describeConnection,
    detailUrl,
    encodeNotificationId,
    failure,
    interpretSave,
    interpretToolResponse,
    isInsecureRemoteOrigin,
    mcpEndpoint,
    normalizeOrigin,
    normalizeSettings,
    normalizeToken,
    notificationDestination,
    parseRpcBody,
    performSave,
    permissionPattern,
    saveFeedback,
    tokenPreview,
  });

  Object.defineProperty(globalThis, "WebHubSave", {
    configurable: false,
    enumerable: false,
    value: api,
    writable: false,
  });
})();
