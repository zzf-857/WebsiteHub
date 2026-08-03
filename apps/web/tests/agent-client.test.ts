import assert from "node:assert/strict";
import test from "node:test";

import {
  AgentApiError,
  buildAgentConversationLink,
  confirmAgentSiteDraft,
  conversationTimezoneOffsetMinutes,
  DEFAULT_CONVERSATION_PAGE_SIZE,
  deleteAgentConversation,
  listAllAgentConversations,
  listAgentConversations,
  loadAgentConversation,
  renameAgentConversation,
} from "../lib/agent-client.ts";

test("conversation share links keep only the controlled conversation parameter", () => {
  const result = buildAgentConversationLink(
    "https://webhub.test/library?ask=hello&trace=debug#private-fragment",
    "conversation / 1",
  );
  const url = new URL(result);
  assert.equal(url.origin, "https://webhub.test");
  assert.equal(url.pathname, "/library");
  assert.deepEqual([...url.searchParams], [["c", "conversation / 1"]]);
  assert.equal(url.hash, "");
});

const conversation = {
  id: "conv-1",
  title: "如何整理网址库",
  title_is_custom: false,
  version: 2,
  message_count: 4,
  last_message_at: "2026-07-26T10:00:00Z",
};

const historyPayload = {
  groups: [{ key: "today", label: "今天", items: [conversation] }],
  next_cursor: "cursor-2",
  total_count: 8,
};

const storedMessage = {
  id: "message-1",
  role: "assistant",
  content: "已为你找到相关网站",
  sources: [
    { toolCallId: "call-1", name: "search_library", result: { items: [] } },
  ],
  status: "complete",
};

const libraryCategory = {
  id: "category-1",
  name: "开发",
  is_default: false,
  icon: "Code",
  site_count: 3,
};
const libraryTag = { id: "tag-react", name: "react", site_count: 2 };

const librarySite = {
  id: "site-1",
  name: "MDN",
  original_url: "https://developer.mozilla.org/",
  identity_url: "https://developer.mozilla.org",
  description: null,
  favicon_url: null,
  category: { id: "category-1", name: "开发", is_default: false, icon: "Code" },
  tags: [],
  pinned: false,
  source: "manual",
  analysis_status: "not_analyzed",
  analysis_phase: null,
  version: 1,
  created_at: "2026-07-25T10:00:00Z",
  updated_at: "2026-07-25T10:00:00Z",
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fakeNow(timezoneOffset: number): Date {
  return { getTimezoneOffset: () => timezoneOffset } as unknown as Date;
}

type RecordedCall = { method: string; url: string; body?: Record<string, unknown> };

// 按 URL + method 分派的网址库接口打桩：既返回可通过 contract 归一化的响应，
// 也把调用顺序完整记录下来，供 confirmAgentSiteDraft 的编排断言使用。
function stubLibraryFetch(
  options: { categories?: unknown[]; tags?: unknown[] } = {},
): RecordedCall[] {
  const calls: RecordedCall[] = [];
  let createdCategories = 0;
  let createdTags = 0;
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, ...(body !== undefined ? { body } : {}) });
    if (url === "/api/backend/library/categories" && method === "GET") {
      return jsonResponse(options.categories ?? []);
    }
    if (url === "/api/backend/library/categories" && method === "POST") {
      createdCategories += 1;
      return jsonResponse({
        id: `category-new-${createdCategories}`,
        name: body.name,
        is_default: false,
        icon: "Folder",
        site_count: 0,
      }, 201);
    }
    if (url === "/api/backend/library/tags" && method === "GET") {
      return jsonResponse(options.tags ?? []);
    }
    if (url === "/api/backend/library/tags" && method === "POST") {
      createdTags += 1;
      return jsonResponse({ id: `tag-new-${createdTags}`, name: body.name, site_count: 0 }, 201);
    }
    if (url === "/api/backend/library/sites" && method === "POST") {
      return jsonResponse(librarySite, 201);
    }
    throw new Error(`意外的请求：${method} ${url}`);
  };
  return calls;
}

test("conversation list keeps cookies, clamps limit and normalizes groups", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ input: String(input), init });
    return jsonResponse(historyPayload);
  };

  const history = await listAgentConversations();
  await listAgentConversations({ cursor: " cursor-2 ", limit: 999 });
  await listAgentConversations({ limit: 0 });

  const first = new URL(requests[0]?.input ?? "", "http://localhost");
  assert.equal(first.pathname, "/api/backend/conversations");
  assert.equal(first.searchParams.get("limit"), String(DEFAULT_CONVERSATION_PAGE_SIZE));
  assert.equal(
    first.searchParams.get("timezone_offset_minutes"),
    String(conversationTimezoneOffsetMinutes()),
  );
  assert.equal(first.searchParams.has("cursor"), false);
  assert.equal(requests[0]?.init?.credentials, "include");
  assert.equal(requests[0]?.init?.cache, "no-store");

  const second = new URL(requests[1]?.input ?? "", "http://localhost");
  assert.equal(second.searchParams.get("cursor"), "cursor-2");
  assert.equal(second.searchParams.get("limit"), "100");
  const third = new URL(requests[2]?.input ?? "", "http://localhost");
  assert.equal(third.searchParams.get("limit"), "1");

  assert.equal(history.groups[0]?.key, "today");
  assert.equal(history.groups[0]?.label, "今天");
  assert.equal(history.groups[0]?.items[0]?.titleIsCustom, false);
  assert.equal(history.groups[0]?.items[0]?.messageCount, 4);
  assert.equal(history.nextCursor, "cursor-2");
  assert.equal(history.totalCount, 8);
});

test("timezone offset is truncated and clamped to the ±14 hour range", () => {
  assert.equal(conversationTimezoneOffsetMinutes(fakeNow(-900)), 840);
  assert.equal(conversationTimezoneOffsetMinutes(fakeNow(900)), -840);
  assert.equal(conversationTimezoneOffsetMinutes(fakeNow(-330.9)), 330);
});

test("conversation detail encodes ids and rejects blank ids before fetching", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: string[] = [];
  globalThis.fetch = async (input) => {
    requests.push(String(input));
    return jsonResponse({ conversation, messages: [storedMessage], next_cursor: null });
  };

  const detail = await loadAgentConversation(" conv/one ");
  assert.equal(requests[0], "/api/backend/conversations/conv%2Fone?limit=100");
  assert.equal(detail.conversation.id, "conv-1");
  assert.equal(detail.messages[0]?.status, "complete");
  const firstSource = detail.messages[0]?.sources[0];
  assert.equal(firstSource && "name" in firstSource ? firstSource.name : null, "search_library");

  await assert.rejects(loadAgentConversation("   "), (error: unknown) => {
    assert.ok(error instanceof TypeError);
    assert.match(error.message, /会话 ID 不能为空/);
    return true;
  });
  assert.equal(requests.length, 1);
});

test("conversation history and detail consume every cursor page without duplicates", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: string[] = [];
  globalThis.fetch = async (input) => {
    const url = String(input);
    requests.push(url);
    const parsed = new URL(url, "http://localhost");
    const cursor = parsed.searchParams.get("cursor");
    if (parsed.pathname === "/api/backend/conversations") {
      return cursor === null
        ? jsonResponse({
            groups: [{ key: "today", label: "今天", items: [conversation] }],
            next_cursor: "history-2",
            total_count: 2,
          })
        : jsonResponse({
            groups: [{
              key: "today",
              label: "今天",
              items: [conversation, { ...conversation, id: "conv-2", title: "第二个会话" }],
            }],
            next_cursor: null,
            total_count: 2,
          });
    }
    return cursor === null
      ? jsonResponse({
          conversation,
          messages: [storedMessage],
          next_cursor: "messages-2",
        })
      : jsonResponse({
          conversation,
          messages: [storedMessage, { ...storedMessage, id: "message-2", content: "第二页" }],
          next_cursor: null,
        });
  };

  const history = await listAllAgentConversations();
  const detail = await loadAgentConversation("conv-1");

  assert.deepEqual(history.groups[0]?.items.map((item) => item.id), ["conv-1", "conv-2"]);
  assert.equal(history.nextCursor, null);
  assert.deepEqual(detail.messages.map((message) => message.id), ["message-1", "message-2"]);
  assert.equal(detail.nextCursor, null);
  assert.equal(requests.length, 4);
  assert.match(requests[1] ?? "", /cursor=history-2/u);
  assert.match(requests[3] ?? "", /cursor=messages-2/u);
});

test("rename and delete serialize snake_case optimistic concurrency", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ input: String(input), init });
    return init?.method === "DELETE"
      ? new Response(null, { status: 204 })
      : jsonResponse({ ...conversation, title: "新标题", title_is_custom: true, version: 3 });
  };

  const renamed = await renameAgentConversation("conv/one", "  新标题  ", 2);
  await deleteAgentConversation("conv/one", 3);

  assert.equal(requests[0]?.input, "/api/backend/conversations/conv%2Fone");
  assert.equal(requests[0]?.init?.method, "PATCH");
  assert.equal(new Headers(requests[0]?.init?.headers).get("content-type"), "application/json");
  assert.deepEqual(JSON.parse(String(requests[0]?.init?.body)), {
    title: "新标题",
    expected_version: 2,
  });
  assert.equal(renamed.title, "新标题");
  assert.equal(renamed.titleIsCustom, true);
  assert.equal(renamed.version, 3);

  assert.equal(requests[1]?.input, "/api/backend/conversations/conv%2Fone?expected_version=3");
  assert.equal(requests[1]?.init?.method, "DELETE");
  assert.equal(requests[1]?.init?.body, undefined);
});

test("surfaces structured backend codes and keeps Chinese fallbacks", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => jsonResponse({
    detail: { code: "version_conflict", message: "会话已被其他窗口修改" },
  }, 409);

  await assert.rejects(
    renameAgentConversation("conv-1", "新标题", 1),
    (error: unknown) => {
      assert.ok(error instanceof AgentApiError);
      assert.equal(error.status, 409);
      assert.equal(error.code, "version_conflict");
      assert.equal(error.message, "会话已被其他窗口修改");
      return true;
    },
  );

  globalThis.fetch = async () => new Response(null, { status: 401 });
  await assert.rejects(
    listAgentConversations(),
    (error: unknown) => {
      assert.ok(error instanceof AgentApiError);
      assert.equal(error.status, 401);
      assert.equal(error.code, undefined);
      assert.equal(error.message, "登录状态已失效，请重新登录");
      return true;
    },
  );
});

test("draft confirmation reuses existing categories and tags without creating", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const calls = stubLibraryFetch({ categories: [libraryCategory], tags: [libraryTag] });

  const site = await confirmAgentSiteDraft({
    url: "https://react.dev/",
    name: "React 官方文档",
    description: "",
    category: " 开发 ",
    tags: [" React ", "react", "REACT"],
  });

  assert.deepEqual(calls.map((call) => `${call.method} ${call.url}`), [
    "GET /api/backend/library/categories",
    "GET /api/backend/library/tags",
    "POST /api/backend/library/sites",
  ]);
  const body = calls[2]?.body ?? {};
  assert.deepEqual(body, {
    name: "React 官方文档",
    url: "https://react.dev/",
    category_id: "category-1",
    tag_ids: ["tag-react"],
    source: "agent",
  });
  assert.equal("description" in body, false);
  assert.equal(site.id, "site-1");
});

test("draft confirmation creates missing categories and tags before the site", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const calls = stubLibraryFetch({ categories: [libraryCategory], tags: [libraryTag] });

  await confirmAgentSiteDraft({
    url: "https://vuejs.org/",
    name: "Vue.js",
    description: "渐进式前端框架",
    category: "前端框架",
    tags: ["React", "Vue", "vue"],
  });

  assert.deepEqual(calls.map((call) => `${call.method} ${call.url}`), [
    "GET /api/backend/library/categories",
    "POST /api/backend/library/categories",
    "GET /api/backend/library/tags",
    "POST /api/backend/library/tags",
    "POST /api/backend/library/sites",
  ]);
  assert.deepEqual(calls[1]?.body, { name: "前端框架" });
  // 大小写去重基于 Map 覆盖语义：同一标签保留最后一次出现的写法。
  assert.deepEqual(calls[3]?.body, { name: "vue" });
  assert.deepEqual(calls[4]?.body, {
    name: "Vue.js",
    url: "https://vuejs.org/",
    description: "渐进式前端框架",
    category_id: "category-new-1",
    tag_ids: ["tag-react", "tag-new-1"],
    source: "agent",
  });
});

test("draft confirmation skips category and tag endpoints when the draft has none", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const calls = stubLibraryFetch();

  await confirmAgentSiteDraft({
    url: "https://example.com/",
    name: "示例站点",
    description: "",
    category: "  ",
    tags: ["  ", ""],
  });

  assert.deepEqual(calls.map((call) => `${call.method} ${call.url}`), [
    "POST /api/backend/library/sites",
  ]);
  assert.deepEqual(calls[0]?.body, {
    name: "示例站点",
    url: "https://example.com/",
    source: "agent",
  });
});
