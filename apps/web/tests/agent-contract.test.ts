import assert from "node:assert/strict";
import test from "node:test";

import {
  AgentContractError,
  agentErrorDetails,
  agentSourceLabels,
  agentToolLabel,
  describeAgentToolResult,
  latestAgentUserText,
  MAX_AGENT_MESSAGE_LENGTH,
  normalizeAgentConversationHistory,
  normalizeAgentMarkdownLink,
  normalizeAgentMessageMetadata,
  normalizeAgentStreamError,
  normalizeAgentToolCall,
  normalizeAgentToolResult,
  prepareAgentChatRequest,
  toAgentUIMessages,
} from "../lib/agent-contract.ts";

test("builds snake-case chat request bodies without leaking optional keys", () => {
  assert.deepEqual(
    prepareAgentChatRequest({ message: "  帮我找前端文档  ", conversationId: "conv-1" }),
    { message: "帮我找前端文档", conversation_id: "conv-1" },
  );

  const withoutConversation = prepareAgentChatRequest({ message: "你好" });
  assert.deepEqual(withoutConversation, { message: "你好" });
  assert.equal("conversation_id" in withoutConversation, false);
  assert.equal("conversation_id" in prepareAgentChatRequest({ message: "你好", conversationId: null }), false);
  assert.equal("conversation_id" in prepareAgentChatRequest({ message: "你好", conversationId: "   " }), false);

  assert.equal("metadata" in prepareAgentChatRequest({ message: "你好", metadata: {} }), false);
  assert.deepEqual(
    prepareAgentChatRequest({ message: "你好", metadata: { web_search: true } }),
    { message: "你好", metadata: { web_search: true } },
  );
});

test("rejects blank messages and enforces the length limit in code points", () => {
  assert.throws(
    () => prepareAgentChatRequest({ message: "   " }),
    (error: unknown) => error instanceof AgentContractError && /不能为空/.test(error.message),
  );

  const atLimit = "😀".repeat(MAX_AGENT_MESSAGE_LENGTH);
  assert.ok(atLimit.length > MAX_AGENT_MESSAGE_LENGTH);
  assert.equal(prepareAgentChatRequest({ message: atLimit }).message, atLimit);

  assert.throws(
    () => prepareAgentChatRequest({ message: "😀".repeat(MAX_AGENT_MESSAGE_LENGTH + 1) }),
    (error: unknown) =>
      error instanceof AgentContractError &&
      new RegExp(String(MAX_AGENT_MESSAGE_LENGTH)).test(error.message),
  );
});

test("extracts the latest user text from text parts only", () => {
  assert.equal(
    latestAgentUserText([
      { role: "user", parts: [{ type: "text", text: "第一个问题" }] },
      { role: "assistant", parts: [{ type: "text", text: "助手的回答" }] },
      {
        role: "user",
        parts: [
          { type: "text", text: "帮我找 " },
          { type: "reasoning", text: "不应出现在结果里" },
          { type: "text", text: "React 文档" },
        ],
      },
      { role: "assistant", parts: [{ type: "text", text: "稍等" }] },
    ]),
    "帮我找 React 文档",
  );

  assert.equal(
    latestAgentUserText([
      { role: "user", parts: [{ type: "text", text: "早一点的问题" }] },
      { role: "user", parts: [{ type: "text", text: "   " }] },
    ]),
    "早一点的问题",
  );

  assert.equal(latestAgentUserText([]), "");
  assert.equal(latestAgentUserText([{ role: "assistant", parts: [{ type: "text", text: "只有助手" }] }]), "");
  assert.equal(latestAgentUserText([{ role: "user" }, { role: "user", parts: [{ type: "file", url: "x" }] }]), "");
});

test("projects tool results into links, facets, drafts, errors, and safe compatibility views", () => {
  assert.deepEqual(
    describeAgentToolResult("search_library", {
      source: "站内存储数据",
      matched_count: 2,
      items: [
        {
          site_id: "site-1",
          name: "MDN",
          url: "https://developer.mozilla.org/",
          favicon_url: "https://developer.mozilla.org/favicon.ico",
          description: "Web 文档",
          category: "开发",
          tags: ["文档", 3, "  前端  "],
          pinned: true,
        },
        { title: "仅有标题", snippet: "来自搜索摘要", url: "https://example.com/" },
      ],
    }),
    {
      kind: "links",
      source: "站内存储数据",
      matchedCount: 2,
      items: [
        {
          siteId: "site-1",
          name: "MDN",
          url: "https://developer.mozilla.org/",
          faviconUrl: "https://developer.mozilla.org/favicon.ico",
          description: "Web 文档",
          category: "开发",
          tags: ["文档", "前端"],
          pinned: true,
        },
        {
          siteId: null,
          name: "仅有标题",
          url: "https://example.com/",
          faviconUrl: null,
          description: "来自搜索摘要",
          category: null,
          tags: [],
          pinned: false,
        },
      ],
    },
  );

  assert.deepEqual(
    describeAgentToolResult("list_tags", {
      source: "站内存储数据",
      items: [{ id: "tag-1", name: "文档", site_count: 3 }, { name: "无编号" }],
    }),
    {
      kind: "facets",
      source: "站内存储数据",
      items: [
        { id: "tag-1", name: "文档", count: 3 },
        { id: "无编号", name: "无编号", count: null },
      ],
    },
  );

  assert.deepEqual(
    describeAgentToolResult("propose_site", {
      status: "awaiting_confirmation",
      draft: {
        url: "https://example.com/docs",
        name: "示例文档站",
        description: "  一个示例  ",
        category: "开发",
        tags: ["工具", "  文档  "],
      },
      duplicate: { site_id: "site-1", name: "已收录的站点", url: "https://example.com/docs", pinned: true },
    }),
    {
      kind: "draft",
      draft: {
        url: "https://example.com/docs",
        name: "示例文档站",
        description: "一个示例",
        category: "开发",
        tags: ["工具", "文档"],
      },
      duplicate: {
        siteId: "site-1",
        name: "已收录的站点",
        url: "https://example.com/docs",
        faviconUrl: null,
        description: null,
        category: null,
        tags: [],
        pinned: true,
      },
    },
  );

  assert.deepEqual(
    describeAgentToolResult("propose_site", { status: "rejected", reason: "该网址无法访问" }),
    { kind: "rejected", reason: "该网址无法访问" },
  );

  assert.deepEqual(
    describeAgentToolResult("web_search", { source: "联网搜索", error: "  没有可用的搜索结果  " }),
    { kind: "error", source: "联网搜索", message: "没有可用的搜索结果" },
  );

  const legacyMessage = "这条结果来自旧版本，当前界面无法安全展示，请重新执行。";
  assert.deepEqual(describeAgentToolResult("unknown_tool", { unexpected: true }), {
    kind: "unavailable",
    message: legacyMessage,
  });
  assert.deepEqual(describeAgentToolResult("unknown_tool", null), {
    kind: "unavailable",
    message: legacyMessage,
  });
  assert.deepEqual(describeAgentToolResult("unknown_tool", "  纯文本结果  "), {
    kind: "unavailable",
    message: legacyMessage,
  });
  const unknown = describeAgentToolResult("unknown_tool", { secret: "raw-json" });
  assert.equal(unknown.kind, "unavailable");
  if (unknown.kind === "unavailable") assert.doesNotMatch(unknown.message, /raw-json/u);
});

test("accepts explicit safe Markdown links and rejects executable or ambiguous hrefs", () => {
  assert.deepEqual(normalizeAgentMarkdownLink("https://docs.example.com/path"), {
    kind: "external",
    href: "https://docs.example.com/path",
    hostname: "docs.example.com",
  });
  assert.deepEqual(normalizeAgentMarkdownLink("/library/site-1"), {
    kind: "internal",
    href: "/library/site-1",
  });
  assert.deepEqual(normalizeAgentMarkdownLink("#section"), {
    kind: "internal",
    href: "#section",
  });
  for (const unsafe of [
    "//evil.example",
    "/\\evil.example",
    "javascript:alert(1)",
    "data:text/html,hello",
    "https://safe.example/\nunsafe",
  ]) {
    assert.equal(normalizeAgentMarkdownLink(unsafe), null);
  }
});

test("drops non-http urls to null while keeping named entries", () => {
  assert.deepEqual(
    describeAgentToolResult("search_library", {
      source: "站内存储数据",
      matched_count: 3,
      items: [
        { name: "脚本协议站点", url: "javascript:alert(1)" },
        { url: "data:text/html,hi" },
        { name: "正常站点", url: "https://example.com/safe" },
      ],
    }),
    {
      kind: "links",
      source: "站内存储数据",
      matchedCount: 3,
      items: [
        {
          siteId: null,
          name: "脚本协议站点",
          url: null,
          faviconUrl: null,
          description: null,
          category: null,
          tags: [],
          pinned: false,
        },
        {
          siteId: null,
          name: "正常站点",
          url: "https://example.com/safe",
          faviconUrl: null,
          description: null,
          category: null,
          tags: [],
          pinned: false,
        },
      ],
    },
  );
});

test("labels answer provenance from tool results and falls back to the model label", () => {
  const libraryResult = {
    toolCallId: "call-1",
    name: "search_library",
    result: { source: "站内存储数据", matched_count: 1, items: [] },
  };
  const webResult = {
    toolCallId: "call-2",
    name: "web_search",
    result: { source: "联网搜索", items: [] },
  };

  assert.deepEqual(agentSourceLabels([libraryResult]), ["站内存储数据"]);
  assert.deepEqual(agentSourceLabels([libraryResult, webResult, libraryResult]), [
    "站内存储数据",
    "联网搜索",
  ]);
  assert.deepEqual(agentSourceLabels([]), ["llm推荐"]);
});

test("returns null for malformed stream chunks and falls back to the tool name as call id", () => {
  assert.equal(normalizeAgentToolCall(null), null);
  assert.equal(normalizeAgentToolCall(["search_library"]), null);
  assert.equal(normalizeAgentToolCall({ arguments: {} }), null);
  assert.deepEqual(normalizeAgentToolCall({ name: "search_library", arguments: { q: "文档" } }), {
    toolCallId: "search_library",
    name: "search_library",
    arguments: { q: "文档" },
  });
  assert.deepEqual(normalizeAgentToolCall({ toolCallId: "call-1", name: "search_library", arguments: "oops" }), {
    toolCallId: "call-1",
    name: "search_library",
    arguments: {},
  });

  assert.equal(normalizeAgentToolResult(null), null);
  assert.equal(normalizeAgentToolResult([]), null);
  assert.equal(normalizeAgentToolResult({ result: {} }), null);
  assert.deepEqual(normalizeAgentToolResult({ name: "web_search", result: { items: [] } }), {
    toolCallId: "web_search",
    name: "web_search",
    result: { items: [] },
  });

  assert.equal(normalizeAgentStreamError(null), null);
  assert.equal(normalizeAgentStreamError([]), null);
  assert.equal(normalizeAgentStreamError({ code: "rate_limited" }), null);
  assert.deepEqual(normalizeAgentStreamError({ message: "  服务暂时不可用  " }), {
    code: "agent_error",
    message: "服务暂时不可用",
  });
});

test("keeps only the known metadata keys and requires webSearch to be boolean", () => {
  assert.deepEqual(
    normalizeAgentMessageMetadata({
      conversationId: "  conv-1  ",
      provider: "account-provider",
      model: "chat-model",
      webSearch: false,
      errorCode: "rate_limited",
      elapsedMs: 2_450,
      timeToFirstTokenMs: 310,
      reasoningMs: 1_200,
      usage: {
        inputTokens: 80,
        outputTokens: 20,
        totalTokens: 100,
        reasoningTokens: 12,
        estimatedCost: 99,
      },
      injected: "应被忽略",
    }),
    {
      conversationId: "conv-1",
      provider: "account-provider",
      model: "chat-model",
      webSearch: false,
      errorCode: "rate_limited",
      elapsedMs: 2_450,
      timeToFirstTokenMs: 310,
      reasoningMs: 1_200,
      usage: {
        inputTokens: 80,
        outputTokens: 20,
        totalTokens: 100,
        reasoningTokens: 12,
      },
    },
  );

  assert.deepEqual(normalizeAgentMessageMetadata({ webSearch: "true" }), {});
  assert.deepEqual(normalizeAgentMessageMetadata({ webSearch: 1 }), {});
  assert.deepEqual(
    normalizeAgentMessageMetadata({
      elapsedMs: -1,
      timeToFirstTokenMs: 1.5,
      reasoningMs: "200",
      usage: { inputTokens: -2, totalTokens: "100" },
    }),
    {},
  );
  assert.deepEqual(normalizeAgentMessageMetadata(null), {});
});

test("parses grouped conversation history and rejects non-array groups", () => {
  assert.deepEqual(
    normalizeAgentConversationHistory({
      groups: [
        {
          key: "today",
          label: "今天",
          items: [
            {
              id: "conv-1",
              title: "前端资料整理",
              title_is_custom: false,
              version: 2,
              message_count: 6,
              last_message_at: "2026-07-26T09:00:00Z",
            },
          ],
        },
      ],
      next_cursor: "cursor-2",
      total_count: 18,
    }),
    {
      groups: [
        {
          key: "today",
          label: "今天",
          items: [
            {
              id: "conv-1",
              title: "前端资料整理",
              titleIsCustom: false,
              version: 2,
              messageCount: 6,
              lastMessageAt: "2026-07-26T09:00:00Z",
            },
          ],
        },
      ],
      nextCursor: "cursor-2",
      totalCount: 18,
    },
  );

  assert.deepEqual(normalizeAgentConversationHistory({ groups: [], next_cursor: null, total_count: 0 }), {
    groups: [],
    nextCursor: null,
    totalCount: 0,
  });

  assert.throws(
    () => normalizeAgentConversationHistory({ groups: "not-an-array", next_cursor: null, total_count: 0 }),
    (error: unknown) => error instanceof AgentContractError && /groups/.test(error.message),
  );
});

test("restores archived reasoning, provenance, metadata, and answer text in live order", () => {
  assert.deepEqual(
    toAgentUIMessages([
      {
        id: "m-1", role: "system", content: "系统提示", parts: [], sources: [], metadata: {}, status: "complete",
      },
      {
        id: "m-2", role: "user", content: "帮我找文档", parts: [], sources: [], metadata: {}, status: "complete",
      },
      {
        id: "m-3", role: "tool", content: "工具输出", parts: [], sources: [], metadata: {}, status: "complete",
      },
      {
        id: "m-4",
        role: "assistant",
        content: "给你两个链接",
        parts: [
          { type: "reasoning", text: "先查网址库。" },
          { type: "text", text: "给你两个链接" },
        ],
        sources: [{ toolCallId: "call-1", name: "search_library", result: { items: [] } }],
        metadata: { elapsedMs: 900, usage: { totalTokens: 24 } },
        status: "complete",
      },
      {
        id: "m-5", role: "assistant", content: "", parts: [], sources: [], metadata: {}, status: "aborted",
      },
    ]),
    [
      { id: "m-2", role: "user", parts: [{ type: "text", text: "帮我找文档" }] },
      {
        id: "m-4",
        role: "assistant",
        metadata: { elapsedMs: 900, usage: { totalTokens: 24 } },
        parts: [
          { type: "reasoning", text: "先查网址库。" },
          {
            type: "data-agent-tool-result",
            data: { toolCallId: "call-1", name: "search_library", result: { items: [] } },
          },
          { type: "text", text: "给你两个链接" },
        ],
      },
    ],
  );
});

test("extracts structured error details and falls back to readable messages", () => {
  assert.deepEqual(
    agentErrorDetails(409, { detail: { code: "  version_conflict  ", message: "  会话已被更新  " } }),
    { code: "version_conflict", message: "会话已被更新" },
  );
  assert.deepEqual(agentErrorDetails(404, { detail: { code: "not_found" } }), {
    code: "not_found",
    message: "会话不存在或已被删除",
  });
  assert.equal(agentErrorDetails(422, { detail: [{ msg: "Invalid field" }] }).message, "Invalid field");
  assert.equal(agentErrorDetails(401, "not-json").message, "登录状态已失效，请重新登录");
  assert.deepEqual(agentErrorDetails(503, {}), { message: "Agent 服务暂时不可用，请稍后重试" });
  assert.deepEqual(agentErrorDetails(500, null), { message: "Agent 服务暂时不可用，请稍后重试" });
});


function siteUpdateResult(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    status: "awaiting_confirmation",
    message: "修改草稿已生成，等待用户在界面上确认后才会写入。",
    draft: {
      kind: "site_update",
      site_id: "site-1",
      expected_version: 4,
      before: {
        site_id: "site-1",
        name: "Figma",
        url: "https://figma.com",
        description: "界面设计工具",
        category: "未分类",
        tags: [],
        pinned: false,
      },
      changes: { category: "设计", pinned: true },
      after: {
        site_id: "site-1",
        name: "Figma",
        url: "https://figma.com",
        description: "界面设计工具",
        category: "设计",
        tags: [],
        pinned: true,
      },
      ...overrides,
    },
  };
}

test("修改草稿被投影成带改前/改后的 site-update 视图", () => {
  const view = describeAgentToolResult("propose_site_update", siteUpdateResult());
  assert.equal(view.kind, "site-update");
  if (view.kind !== "site-update") return;
  assert.equal(view.draft.siteId, "site-1");
  // 乐观锁令牌必须原样带到前端，否则确认时无法检测冲突。
  assert.equal(view.draft.expectedVersion, 4);
  assert.deepEqual(view.draft.changes, { category: "设计", pinned: true });
  assert.equal(view.draft.before.pinned, false);
  assert.equal(view.draft.after.category, "设计");
});

test("changes 里的空字符串与 false 是合法改动，不能被当成未改", () => {
  const view = describeAgentToolResult(
    "propose_site_update",
    siteUpdateResult({ changes: { description: "", pinned: false } }),
  );
  assert.equal(view.kind, "site-update");
  if (view.kind !== "site-update") return;
  // 用 typeof 判断而不是真值判断：清空说明、取消置顶都必须能表达。
  assert.deepEqual(view.draft.changes, { description: "", pinned: false });
});

test("缺少乐观锁版本或改动集合的修改草稿降级为安全提示，绝不半渲染", () => {
  const noVersion = describeAgentToolResult(
    "propose_site_update",
    siteUpdateResult({ expected_version: 0 }),
  );
  assert.equal(noVersion.kind, "unavailable");

  const noChanges = describeAgentToolResult(
    "propose_site_update",
    siteUpdateResult({ changes: {} }),
  );
  assert.equal(noChanges.kind, "unavailable");
});

test("无需修改时给出 noop 而不是一张点了没用的确认卡", () => {
  const view = describeAgentToolResult("propose_site_update", {
    status: "noop",
    message: "该网站当前已经是这个状态，没有需要修改的内容。",
    site: { name: "Figma" },
  });
  assert.equal(view.kind, "noop");
  if (view.kind !== "noop") return;
  assert.match(view.message, /没有需要修改/u);
});

test("Space 变更草稿被投影成 space-membership 视图", () => {
  const view = describeAgentToolResult("propose_space_membership", {
    status: "awaiting_confirmation",
    draft: {
      kind: "space_membership",
      action: "add",
      site_id: "site-1",
      site_name: "Figma",
      space_id: "space-1",
      space_name: "设计",
      expected_version: 2,
    },
  });
  assert.equal(view.kind, "space-membership");
  if (view.kind !== "space-membership") return;
  assert.equal(view.draft.action, "add");
  assert.equal(view.draft.spaceName, "设计");
  assert.equal(view.draft.expectedVersion, 2);
});

test("非法 action 的 Space 草稿降级为安全提示", () => {
  const view = describeAgentToolResult("propose_space_membership", {
    status: "awaiting_confirmation",
    draft: {
      action: "delete_everything",
      site_id: "site-1",
      site_name: "Figma",
      space_id: "space-1",
      space_name: "设计",
      expected_version: 2,
    },
  });
  assert.equal(view.kind, "unavailable");
});

test("找不到 Space 时把已有 Space 一并说清楚", () => {
  const view = describeAgentToolResult("propose_space_membership", {
    status: "rejected",
    reason: "没有找到名为“不存在”的 Space。",
    available_spaces: ["设计", "阅读"],
  });
  assert.equal(view.kind, "rejected");
  if (view.kind !== "rejected") return;
  assert.match(view.reason, /设计、阅读/u);
});

test("两个新工具都有中文标签，不会在界面上露出裸函数名", () => {
  assert.equal(agentToolLabel("propose_site_update"), "生成修改草稿");
  assert.equal(agentToolLabel("propose_space_membership"), "生成 Space 变更草稿");
});

test("批量草稿被投影成 site-batch 视图，逐项状态原样保留", () => {
  const view = describeAgentToolResult("propose_sites", {
    status: "awaiting_confirmation",
    draft: {
      kind: "site_batch",
      urls: ["https://a.example.com/1", "https://b.example.com/2"],
      total: 4,
      ready: 2,
      duplicate: 1,
      invalid: 1,
      items: [
        { url: "https://a.example.com/1", status: "ready", reason: null },
        { url: "https://b.example.com/2", status: "ready", reason: null },
        { url: "https://dup.example.com", status: "duplicate", reason: "网址库里已经有这个网址" },
        { url: "ftp://bad.example.com", status: "invalid", reason: "网址无效或不受支持" },
      ],
    },
  });
  assert.equal(view.kind, "site-batch");
  if (view.kind !== "site-batch") return;
  assert.equal(view.draft.ready, 2);
  assert.equal(view.draft.duplicate, 1);
  assert.equal(view.draft.invalid, 1);
  assert.deepEqual(view.draft.urls, ["https://a.example.com/1", "https://b.example.com/2"]);
  assert.equal(view.draft.items.length, 4);
});

test("一条都不会写入的批量草稿降级，不渲染点了等于没点的确认卡", () => {
  const view = describeAgentToolResult("propose_sites", {
    status: "awaiting_confirmation",
    draft: { kind: "site_batch", urls: [], total: 2, ready: 0, duplicate: 2, invalid: 0, items: [] },
  });
  assert.equal(view.kind, "unavailable");
});

test("批量草稿里状态不合法的条目被丢弃，而不是原样渲染", () => {
  const view = describeAgentToolResult("propose_sites", {
    status: "awaiting_confirmation",
    draft: {
      kind: "site_batch",
      urls: ["https://a.example.com/1"],
      total: 2,
      ready: 1,
      duplicate: 0,
      invalid: 0,
      items: [
        { url: "https://a.example.com/1", status: "ready", reason: null },
        { url: "https://b.example.com/2", status: "已经写进去了", reason: null },
      ],
    },
  });
  assert.equal(view.kind, "site-batch");
  if (view.kind !== "site-batch") return;
  assert.deepEqual(view.draft.items.map((item) => item.status), ["ready"]);
});

test("propose_sites 有中文标签", () => {
  assert.equal(agentToolLabel("propose_sites"), "生成批量收录草稿");
});

test("propose_reclassify 草稿正确转换为 reclassify 视图", () => {
  const view = describeAgentToolResult("propose_reclassify", {
    status: "awaiting_confirmation",
    draft: {
      kind: "reclassify",
      site_count: 2,
      estimated_request_count: 1,
      maximum_request_count: 2,
      estimated_input_characters: 2900,
      allowed_categories: ["开发", "工具"],
      expected_categories: { c1: "开发", c2: "工具" },
      expected_versions: { s1: 1, s2: 2 },
    },
  });
  assert.equal(view.kind, "reclassify");
  if (view.kind !== "reclassify") return;
  assert.equal(view.draft.siteCount, 2);
  assert.equal(view.draft.estimatedRequestCount, 1);
  assert.equal(view.draft.maximumRequestCount, 2);
  assert.equal(view.draft.estimatedInputCharacters, 2900);
  assert.deepEqual(view.draft.allowedCategories, ["开发", "工具"]);
  assert.deepEqual(view.draft.expectedCategories, { c1: "开发", c2: "工具" });
});

test("propose_reclassify 拒绝不合法的最大请求数", () => {
  const invalidMaximumRequestCounts = [undefined, 0, 1.5, 1];

  for (const maximumRequestCount of invalidMaximumRequestCounts) {
    const view = describeAgentToolResult("propose_reclassify", {
      status: "awaiting_confirmation",
      draft: {
        kind: "reclassify",
        site_count: 2,
        estimated_request_count: 2,
        ...(maximumRequestCount === undefined
          ? {}
          : { maximum_request_count: maximumRequestCount }),
        estimated_input_characters: 2900,
        allowed_categories: ["开发", "工具"],
        expected_categories: { c1: "开发", c2: "工具" },
        expected_versions: { s1: 1, s2: 2 },
      },
    });

    assert.equal(view.kind, "unavailable");
    if (view.kind === "unavailable") {
      assert.equal(view.message, "这条旧版重分类草稿已失效，请重新发起。");
    }
  }
});

test("propose_reclassify 拒绝不完整的网站或分类快照", () => {
  const baseDraft = {
    kind: "reclassify",
    site_count: 2,
    estimated_request_count: 1,
    maximum_request_count: 2,
    estimated_input_characters: 2900,
    allowed_categories: ["开发", "工具"],
    expected_categories: { c1: "开发", c2: "工具" },
    expected_versions: { s1: 1, s2: 2 },
  };
  const invalidDrafts = [
    { ...baseDraft, expected_categories: { c1: "开发" } },
    { ...baseDraft, expected_categories: { c1: "开发", c2: 2 } },
    { ...baseDraft, expected_versions: { s1: 1 } },
    { ...baseDraft, expected_versions: { s1: 0, s2: 2 } },
  ];

  for (const draft of invalidDrafts) {
    const view = describeAgentToolResult("propose_reclassify", {
      status: "awaiting_confirmation",
      draft,
    });
    assert.equal(view.kind, "unavailable");
    if (view.kind === "unavailable") {
      assert.equal(view.message, "这条旧版重分类草稿已失效，请重新发起。");
    }
  }
});

test("propose_reclassify 拒绝与 noop 状态正常处理", () => {
  const rejected = describeAgentToolResult("propose_reclassify", {
    status: "rejected",
    reason: "当前账号未配置模型 Provider",
  });
  assert.equal(rejected.kind, "rejected");
  if (rejected.kind === "rejected") {
    assert.equal(rejected.reason, "当前账号未配置模型 Provider");
  }

  const noop = describeAgentToolResult("propose_reclassify", {
    status: "noop",
    message: "网址库中没有需要分类的网站。",
  });
  assert.equal(noop.kind, "noop");
});
