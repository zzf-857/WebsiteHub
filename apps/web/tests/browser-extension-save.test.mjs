// Q47：浏览器扩展「收藏当前页」的纯逻辑测试。
// apps/browser-extension/save-core.js 是扩展里的普通脚本（Service Worker 用 importScripts、
// 选项页用 <script> 加载），这里在当前 realm 里执行一遍，再测它挂到 globalThis 上的 API。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const extensionDir = new URL("../../browser-extension/", import.meta.url);
const sourcePath = new URL("save-core.js", extensionDir);
vm.runInThisContext(readFileSync(sourcePath, "utf8"), { filename: sourcePath.pathname });
const save = globalThis.WebHubSave;

const ORIGIN = "https://box.tail1234.ts.net";
const TOKEN = "whk_abcdefghijklmnopqrstuvwxyz0123456789-_AB";
const RESOURCE_ID = "6f1d2c3b-4a5e-4f60-9b1a-2c3d4e5f6a7b";
const SETTINGS = { origin: ORIGIN, token: TOKEN };

function readExtensionFile(name) {
  return readFileSync(new URL(name, extensionDir), "utf8");
}

function toolResult(payload, isError = false) {
  const text = typeof payload === "string" ? payload : JSON.stringify(payload);
  return JSON.stringify({
    jsonrpc: "2.0",
    id: 1,
    result: { content: [{ type: "text", text }], isError },
  });
}

function savedPayload(alreadySaved, overrides = {}) {
  return {
    already_saved: alreadySaved,
    resource: {
      id: RESOURCE_ID,
      title: "Hermes Agent",
      url: "https://github.com/nous/hermes",
      kind: "repo",
      ...overrides,
    },
    note: "Saved.",
  };
}

function fakeResponse(status, bodyText, type = "basic") {
  return { status, type, text: async () => bodyText };
}

function recordingFetch(response) {
  const calls = [];
  const fetchImpl = async (input, init) => {
    calls.push({ input, init });
    return typeof response === "function" ? response(input, init) : response;
  };
  return { calls, fetchImpl };
}

test("normalizeOrigin keeps only the origin and fills in a sensible scheme", () => {
  assert.equal(save.normalizeOrigin("https://box.tail1234.ts.net/"), ORIGIN);
  assert.equal(save.normalizeOrigin(" box.tail1234.ts.net "), ORIGIN);
  assert.equal(
    save.normalizeOrigin("https://box.tail1234.ts.net/api/backend/mcp?x=1#y"),
    ORIGIN,
  );
  assert.equal(save.normalizeOrigin("localhost:3100"), "http://localhost:3100");
  assert.equal(save.normalizeOrigin("127.0.0.1:3100/settings"), "http://127.0.0.1:3100");
  assert.equal(save.normalizeOrigin("HTTP://LOCALHOST:3100"), "http://localhost:3100");
  for (const bad of ["", "   ", "ftp://example.com", "https://user:pw@example.com",
    "javascript:alert(1)", "https://exa mple.com", 42, null]) {
    assert.equal(save.normalizeOrigin(bad), null, String(bad));
  }
});

test("insecure-origin hint only fires for plain HTTP off this machine", () => {
  assert.equal(save.isInsecureRemoteOrigin("http://192.168.1.20:3100"), true);
  assert.equal(save.isInsecureRemoteOrigin("http://localhost:3100"), false);
  assert.equal(save.isInsecureRemoteOrigin("http://127.0.0.1:3100"), false);
  assert.equal(save.isInsecureRemoteOrigin(ORIGIN), false);
});

test("host permission pattern drops the port so any port on that host matches", () => {
  assert.equal(save.permissionPattern(ORIGIN), "https://box.tail1234.ts.net/*");
  assert.equal(save.permissionPattern("http://localhost:3100"), "http://localhost/*");
  const manifest = JSON.parse(readExtensionFile("manifest.json"));
  for (const origin of [ORIGIN, "http://localhost:3100"]) {
    const scheme = new URL(origin).protocol;
    assert.ok(
      manifest.optional_host_permissions.includes(`${scheme}//*/*`),
      `optional_host_permissions must cover ${origin}`,
    );
  }
});

test("tokens accept a pasted Bearer prefix and reject anything that is not a WebHub token", () => {
  assert.equal(save.normalizeToken(`  Bearer ${TOKEN} `), TOKEN);
  assert.equal(save.normalizeToken(TOKEN), TOKEN);
  assert.equal(save.normalizeToken("sk-abcdefghijklmnopqrstuvwxyz"), null);
  assert.equal(save.normalizeToken("whk_short"), null);
  assert.equal(save.normalizeToken(`${TOKEN} extra`), null);
  assert.equal(save.tokenPreview(TOKEN), "whk_abcd…");
  assert.equal(save.tokenPreview(""), "");
});

test("stored settings must already be normalized", () => {
  assert.deepEqual(save.normalizeSettings(SETTINGS), SETTINGS);
  assert.equal(save.normalizeSettings({ origin: `${ORIGIN}/`, token: TOKEN }), null);
  assert.equal(save.normalizeSettings({ origin: ORIGIN, token: "nope" }), null);
  assert.equal(save.normalizeSettings({ origin: ORIGIN }), null);
  assert.equal(save.normalizeSettings(undefined), null);
  assert.equal(save.normalizeSettings([ORIGIN, TOKEN]), null);
});

test("save arguments carry the page URL and a cleaned-up title", () => {
  assert.deepEqual(
    save.buildSaveArguments({ url: "https://example.com/post?id=1#top", title: "  A\n\tPost  " }),
    { url: "https://example.com/post?id=1#top", title: "A Post" },
  );
  // 标签页还没有标题时浏览器会把网址当标题；这种标题不发，让服务端用主机名并在分析后补全。
  assert.deepEqual(
    save.buildSaveArguments({ url: "https://example.com/a", title: "https://example.com/a" }),
    { url: "https://example.com/a" },
  );
  assert.deepEqual(save.buildSaveArguments({ url: "http://example.com" }), {
    url: "http://example.com/",
  });
  const longTitle = "长".repeat(save.MAX_TITLE_LENGTH + 50);
  const args = save.buildSaveArguments({ url: "https://example.com/", title: longTitle });
  assert.equal(Array.from(args.title).length, save.MAX_TITLE_LENGTH);
  assert.ok(args.title.endsWith("…"));
  for (const url of ["chrome://extensions/", "edge://settings", "about:blank", "file:///etc/hosts",
    "https://u:p@example.com/", `https://example.com/${"a".repeat(save.MAX_URL_LENGTH)}`, "", undefined]) {
    assert.equal(save.buildSaveArguments({ url, title: "x" }), null, String(url));
  }
  assert.equal(save.buildSaveArguments(null), null);
});

test("the JSON-RPC request is a tools/call with a Bearer token and no cookies or redirects", () => {
  const body = save.buildToolCallRequest("r-1", "save_resource", { url: "https://example.com/" });
  assert.deepEqual(body, {
    jsonrpc: "2.0",
    id: "r-1",
    method: "tools/call",
    params: { name: "save_resource", arguments: { url: "https://example.com/" } },
  });
  assert.deepEqual(save.buildToolCallRequest(2, "list_taxonomy").params, {
    name: "list_taxonomy",
    arguments: {},
  });
  const init = save.buildRequestInit(TOKEN, body);
  assert.equal(init.method, "POST");
  assert.equal(init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(init.headers.Accept, "application/json, text/event-stream");
  assert.equal(init.headers["Content-Type"], "application/json");
  assert.equal(init.credentials, "omit");
  assert.equal(init.redirect, "manual");
  assert.equal(init.cache, "no-store");
  assert.deepEqual(JSON.parse(init.body), body);
  assert.equal(save.mcpEndpoint(ORIGIN), `${ORIGIN}/api/backend/mcp`);
});

test("HTTP-level failures map to specific, actionable errors", () => {
  const cases = [
    [{ status: 401, bodyText: '{"detail":"x"}' }, "UNAUTHORIZED"],
    [{ status: 421, bodyText: "" }, "HOST_REJECTED"],
    [{ status: 404, bodyText: "<html>" }, "NOT_WEBHUB"],
    [{ status: 405, bodyText: "" }, "NOT_WEBHUB"],
    [{ status: 0, type: "opaqueredirect", bodyText: "" }, "REDIRECTED"],
    [{ status: 307, bodyText: "" }, "REDIRECTED"],
    [{ status: 502, bodyText: "" }, "UNAVAILABLE"],
    [{ status: 503, bodyText: "" }, "UNAVAILABLE"],
    [{ status: 500, bodyText: "boom" }, "HTTP_ERROR"],
    [{ status: 200, bodyText: "<!doctype html>" }, "BAD_RESPONSE"],
    [{ status: 200, bodyText: "" }, "BAD_RESPONSE"],
  ];
  for (const [response, code] of cases) {
    const outcome = save.interpretToolResponse(response);
    assert.equal(outcome.ok, false, JSON.stringify(response));
    assert.equal(outcome.code, code, JSON.stringify(response));
    assert.equal(outcome.message, save.ERROR_MESSAGES[code]);
  }
  assert.equal(save.interpretToolResponse({ status: 500, bodyText: "" }).detail, "HTTP 500");
});

test("JSON-RPC and tool errors are distinguished, including read-only tokens", () => {
  const rpcError = save.interpretToolResponse({
    status: 400,
    bodyText: JSON.stringify({ jsonrpc: "2.0", id: 1, error: { code: -32602, message: "Bad\nparams" } }),
  });
  assert.deepEqual(rpcError, {
    ok: false,
    code: "RPC_ERROR",
    message: save.ERROR_MESSAGES.RPC_ERROR,
    detail: "Bad params",
  });

  const readOnly = save.interpretToolResponse({
    status: 200,
    bodyText: toolResult(
      "this token is read-only: it can search and read the library but cannot save or change anything.",
      true,
    ),
  });
  assert.equal(readOnly.code, "READ_ONLY");
  assert.equal(readOnly.detail, undefined);

  const toolError = save.interpretToolResponse({
    status: 200,
    bodyText: toolResult("Cannot save this URL: unsupported address", true),
  });
  assert.equal(toolError.code, "TOOL_ERROR");
  assert.equal(toolError.detail, "Cannot save this URL: unsupported address");

  const longDetail = save.interpretToolResponse({
    status: 200,
    bodyText: toolResult("x".repeat(1000), true),
  });
  assert.ok(Array.from(longDetail.detail).length <= 240);
});

test("successful tool results are parsed from JSON or an SSE stream", () => {
  const direct = save.interpretToolResponse({ status: 200, bodyText: toolResult(savedPayload(false)) });
  assert.equal(direct.ok, true);
  assert.equal(direct.payload.resource.id, RESOURCE_ID);

  const sse = `event: message\r\ndata: ${toolResult(savedPayload(true))}\r\n\r\n`;
  const streamed = save.interpretToolResponse({ status: 200, bodyText: sse });
  assert.equal(streamed.ok, true);
  assert.equal(streamed.payload.already_saved, true);

  assert.equal(
    save.interpretToolResponse({ status: 200, bodyText: toolResult("not json") }).code,
    "BAD_RESPONSE",
  );
  assert.equal(
    save.interpretToolResponse({
      status: 200,
      bodyText: JSON.stringify({ jsonrpc: "2.0", id: 1, result: { content: [] } }),
    }).code,
    "BAD_RESPONSE",
  );
});

test("interpretSave turns save_resource payloads into saved / already states", () => {
  const saved = save.interpretSave({ ok: true, payload: savedPayload(false) });
  assert.deepEqual(saved, {
    ok: true,
    state: "saved",
    resource: { id: RESOURCE_ID, title: "Hermes Agent", url: "https://github.com/nous/hermes" },
  });
  assert.equal(save.interpretSave({ ok: true, payload: savedPayload(true) }).state, "already");

  for (const payload of [
    { resource: savedPayload(false).resource },
    { already_saved: "no", resource: savedPayload(false).resource },
    { already_saved: false },
    savedPayload(false, { id: "../../admin" }),
    savedPayload(false, { id: 7 }),
  ]) {
    assert.equal(save.interpretSave({ ok: true, payload }).code, "BAD_RESPONSE");
  }
  const failed = save.failure("UNAUTHORIZED");
  assert.equal(save.interpretSave(failed), failed);
  assert.equal(save.interpretSave(undefined).code, "BAD_RESPONSE");
});

test("callTool posts to the MCP endpoint and folds transport failures into results", async () => {
  const { calls, fetchImpl } = recordingFetch(fakeResponse(200, toolResult(savedPayload(false))));
  const outcome = await save.callTool({
    fetchImpl,
    origin: ORIGIN,
    token: TOKEN,
    name: "save_resource",
    arguments: { url: "https://github.com/nous/hermes" },
    requestId: "abc",
  });
  assert.equal(outcome.ok, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].input, `${ORIGIN}/api/backend/mcp`);
  assert.equal(calls[0].init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.ok(calls[0].init.signal);
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    jsonrpc: "2.0",
    id: "abc",
    method: "tools/call",
    params: { name: "save_resource", arguments: { url: "https://github.com/nous/hermes" } },
  });

  const offline = await save.callTool({
    fetchImpl: async () => {
      throw new TypeError("Failed to fetch");
    },
    origin: ORIGIN,
    token: TOKEN,
    name: "save_resource",
  });
  assert.equal(offline.code, "NETWORK_ERROR");

  const slow = await save.callTool({
    fetchImpl: (_input, init) =>
      new Promise((_resolve, reject) => {
        init.signal.addEventListener("abort", () => reject(new Error("aborted")));
      }),
    origin: ORIGIN,
    token: TOKEN,
    name: "save_resource",
    timeoutMs: 5,
  });
  assert.equal(slow.code, "TIMEOUT");

  const redirected = await save.callTool({
    fetchImpl: async () => ({
      status: 0,
      type: "opaqueredirect",
      text: async () => {
        throw new Error("opaque responses have no body");
      },
    }),
    origin: ORIGIN,
    token: TOKEN,
    name: "save_resource",
  });
  assert.equal(redirected.code, "REDIRECTED");
});

test("performSave checks settings, URL and permission before touching the network", async () => {
  const { calls, fetchImpl } = recordingFetch(fakeResponse(200, toolResult(savedPayload(false))));
  const target = { url: "https://example.com/", title: "Example" };
  const allow = async () => true;

  const notConfigured = await save.performSave({ target, settings: undefined, hasPermission: allow, fetchImpl });
  assert.equal(notConfigured.state, "error");
  assert.equal(notConfigured.code, "NOT_CONFIGURED");
  assert.equal(notConfigured.buttonTitle, "打开设置");

  const unsupported = await save.performSave({
    target: { url: "chrome://newtab/", title: "New Tab" },
    settings: SETTINGS,
    hasPermission: allow,
    fetchImpl,
  });
  assert.equal(unsupported.code, "UNSUPPORTED_URL");
  assert.equal(unsupported.buttonTitle, null);
  assert.equal(unsupported.contextMessage, "New Tab");

  const asked = [];
  const missing = await save.performSave({
    target,
    settings: SETTINGS,
    hasPermission: async (pattern) => {
      asked.push(pattern);
      return false;
    },
    fetchImpl,
  });
  assert.equal(missing.code, "PERMISSION_MISSING");
  assert.deepEqual(asked, ["https://box.tail1234.ts.net/*"]);

  const throwing = await save.performSave({
    target,
    settings: SETTINGS,
    hasPermission: async () => {
      throw new Error("no permissions API");
    },
    fetchImpl,
  });
  assert.equal(throwing.code, "PERMISSION_MISSING");
  assert.equal(calls.length, 0);
});

test("performSave saves the page and points the notification at the detail page", async () => {
  const { calls, fetchImpl } = recordingFetch(fakeResponse(200, toolResult(savedPayload(false))));
  const requested = [];
  const feedback = await save.performSave({
    target: { url: "https://github.com/nous/hermes", title: "nous/hermes: Hermes Agent" },
    settings: SETTINGS,
    hasPermission: async () => true,
    fetchImpl,
    nonce: 99,
    requestId: "req-9",
    onRequest: (args) => requested.push(args),
  });
  assert.deepEqual(requested, [
    { url: "https://github.com/nous/hermes", title: "nous/hermes: Hermes Agent" },
  ]);
  assert.equal(calls.length, 1);
  assert.equal(JSON.parse(calls[0].init.body).params.arguments.title, "nous/hermes: Hermes Agent");
  assert.deepEqual(feedback, {
    state: "saved",
    notificationId: `webhub-save|detail|${RESOURCE_ID}|99`,
    title: "已收藏到 WebHub",
    message: "Hermes Agent",
    contextMessage: "github.com",
    buttonTitle: "打开详情",
  });
  assert.deepEqual(save.notificationDestination(feedback.notificationId, SETTINGS), {
    kind: "url",
    url: `${ORIGIN}/library/${RESOURCE_ID}`,
  });
  // 设置被清掉后，旧通知不再打开任何页面。
  assert.equal(save.notificationDestination(feedback.notificationId, undefined), null);

  const already = await save.performSave({
    target: { url: "https://github.com/nous/hermes" },
    settings: SETTINGS,
    hasPermission: async () => true,
    fetchImpl: recordingFetch(fakeResponse(200, toolResult(savedPayload(true, { title: "" })))).fetchImpl,
    nonce: 1,
  });
  assert.equal(already.state, "already");
  assert.equal(already.title, "之前已经收藏过");
  assert.equal(already.message, "https://github.com/nous/hermes");
});

test("a failing onRequest hook does not block the save", async () => {
  const feedback = await save.performSave({
    target: { url: "https://example.com/" },
    settings: SETTINGS,
    hasPermission: async () => true,
    fetchImpl: recordingFetch(fakeResponse(200, toolResult(savedPayload(false)))).fetchImpl,
    onRequest: () => {
      throw new Error("tab closed");
    },
  });
  assert.equal(feedback.state, "saved");
});

test("errors that need new settings send the notification button to the options page", async () => {
  const readOnly = await save.performSave({
    target: { url: "https://example.com/", title: "Example" },
    settings: SETTINGS,
    hasPermission: async () => true,
    fetchImpl: recordingFetch(
      fakeResponse(200, toolResult("this token is read-only: nope", true)),
    ).fetchImpl,
    nonce: "n1",
  });
  assert.equal(readOnly.state, "error");
  assert.equal(readOnly.code, "READ_ONLY");
  assert.equal(readOnly.title, "收藏失败");
  assert.equal(readOnly.message, save.ERROR_MESSAGES.READ_ONLY);
  assert.equal(readOnly.contextMessage, "Example");
  assert.equal(readOnly.buttonTitle, "打开设置");
  assert.equal(readOnly.notificationId, "webhub-save|options|n1");
  assert.deepEqual(save.notificationDestination(readOnly.notificationId, undefined), {
    kind: "options",
  });

  const unavailable = save.saveFeedback(save.failure("UNAVAILABLE"), {
    url: "https://example.com/a",
    nonce: "n2",
  });
  assert.equal(unavailable.buttonTitle, null);
  assert.equal(unavailable.contextMessage, "example.com");
  assert.equal(save.notificationDestination(unavailable.notificationId, SETTINGS), null);

  const withDetail = save.saveFeedback(save.failure("TOOL_ERROR", "Cannot save this URL"), {});
  assert.equal(withDetail.message, `${save.ERROR_MESSAGES.TOOL_ERROR}（Cannot save this URL）`);
});

test("notification ids round-trip and reject anything else", () => {
  const detail = save.encodeNotificationId({ kind: "detail", resourceId: RESOURCE_ID }, "a|b c");
  assert.equal(detail, `webhub-save|detail|${RESOURCE_ID}|abc`);
  assert.deepEqual(save.decodeNotificationId(detail), { kind: "detail", resourceId: RESOURCE_ID });
  assert.equal(save.decodeNotificationId("webhub-save|detail|../x|1"), null);
  assert.equal(save.decodeNotificationId("other|options|1"), null);
  assert.equal(save.decodeNotificationId(undefined), null);
  assert.equal(save.detailUrl(ORIGIN, "a/b"), null);
});

test("badges follow the spec: ✓ saved, = already saved, ! failed", () => {
  assert.equal(save.badgeFor("saved").text, "✓");
  assert.equal(save.badgeFor("already").text, "=");
  assert.equal(save.badgeFor("error").text, "!");
  assert.equal(save.badgeFor("anything-else").text, "!");
  for (const state of ["pending", "saved", "already", "error"]) {
    assert.match(save.badgeFor(state).color, /^#[0-9a-f]{6}$/);
  }
});

test("describeConnection summarises list_taxonomy for the options page", () => {
  assert.deepEqual(
    save.describeConnection({
      ok: true,
      payload: {
        categories: [{ name: "工具", count: 3 }, { name: "未分类", count: 1 }],
        tags: [{ name: "AI", count: 2 }],
        tags_truncated: true,
        tag_total: 120,
      },
    }),
    { ok: true, text: "连接成功：2 个分类、120 个标签。" },
  );
  assert.deepEqual(save.describeConnection({ ok: true, payload: { categories: [], tags: [] } }), {
    ok: true,
    text: "连接成功：0 个分类、0 个标签。",
  });
  assert.equal(save.describeConnection({ ok: true, payload: {} }).ok, false);
  assert.deepEqual(save.describeConnection(save.failure("UNAUTHORIZED")), {
    ok: false,
    text: save.ERROR_MESSAGES.UNAUTHORIZED,
  });
  assert.equal(
    save.describeConnection(save.failure("HTTP_ERROR", "HTTP 418")).text,
    `${save.ERROR_MESSAGES.HTTP_ERROR}（HTTP 418）`,
  );
});

test("manifest adds the save entry points and keeps the tab-group bridge intact", () => {
  const manifest = JSON.parse(readExtensionFile("manifest.json"));
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.name, "WebHub");
  for (const permission of ["activeTab", "contextMenus", "notifications", "storage", "tabGroups"]) {
    assert.ok(manifest.permissions.includes(permission), permission);
  }
  assert.equal(manifest.host_permissions, undefined, "WebHub hosts are requested at runtime");
  assert.deepEqual(manifest.optional_host_permissions, ["http://*/*", "https://*/*"]);
  assert.equal(manifest.commands._execute_action.suggested_key.default, "Alt+Shift+S");
  assert.equal(manifest.action.default_popup, undefined, "a popup would swallow action clicks");
  assert.equal(manifest.options_ui.page, "options.html");
  assert.deepEqual(manifest.content_scripts, [
    {
      matches: [
        "http://localhost/*",
        "https://localhost/*",
        "http://127.0.0.1/*",
        "https://127.0.0.1/*",
      ],
      js: ["protocol.js", "content-script.js"],
      run_at: "document_start",
    },
  ]);
  assert.equal(manifest.background.service_worker, "service-worker.js");

  const icons = [...Object.values(manifest.icons), ...Object.values(manifest.action.default_icon)];
  for (const icon of icons) {
    const bytes = readFileSync(new URL(icon, extensionDir));
    assert.equal(bytes.subarray(1, 4).toString("ascii"), "PNG", icon);
  }

  const worker = readExtensionFile("service-worker.js");
  assert.match(worker, /^importScripts\("protocol\.js", "save-core\.js", "save-worker\.js"\);$/m);
  const options = readExtensionFile(manifest.options_ui.page);
  assert.ok(options.indexOf('src="save-core.js"') < options.indexOf('src="options.js"'));
  assert.doesNotMatch(options, /<script>(?!<\/script>)/, "MV3 pages cannot run inline scripts");
});
