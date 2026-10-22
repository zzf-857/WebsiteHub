import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";

import {
  FeedsApiError,
  addFeed,
  deleteFeed,
  listFeeds,
  saveFeedItem,
  updateFeedItem,
} from "../lib/feeds-client.ts";

type FetchCall = { input: string; init: RequestInit };

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function withMockFetch(t: TestContext, responses: Response[]): FetchCall[] {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ input: String(input), init: init ?? {} });
    const next = responses.shift();
    if (!next) throw new Error("测试未预置足够的模拟响应");
    return next;
  }) as typeof fetch;
  t.after(() => {
    globalThis.fetch = original;
  });
  return calls;
}

const FEED = {
  id: "f1",
  url: "https://blog.example/rss",
  title: "Blog",
  kind: "rss",
  site_url: null,
  enabled: true,
  last_fetched_at: null,
  next_fetch_at: null,
  consecutive_failures: 0,
  last_error: null,
  item_count: 0,
};

const ITEM = {
  id: "i/1",
  feed_id: "f1",
  url: "https://blog.example/1",
  title: "One",
  summary: "",
  author: null,
  published_at: null,
  audio_url: null,
  duration_seconds: null,
  fetched_at: "2026-10-22T06:00:00Z",
  read: true,
  dismissed: false,
  saved_site_id: "s1",
};

test("列出订阅：同源代理、带 Cookie、不读缓存", async (t) => {
  const calls = withMockFetch(t, [jsonResponse({ items: [FEED] })]);
  const feeds = await listFeeds();
  assert.equal(calls[0].input, "/api/backend/feeds");
  assert.equal(calls[0].init.credentials, "include");
  assert.equal(calls[0].init.cache, "no-store");
  assert.equal(feeds[0].title, "Blog");
});

test("添加订阅：去掉首尾空白后 POST；空地址不发请求", async (t) => {
  const calls = withMockFetch(t, [jsonResponse(FEED, 201)]);
  await addFeed("  https://blog.example/rss ");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, JSON.stringify({ url: "https://blog.example/rss" }));
  await assert.rejects(addFeed("   "), TypeError);
  assert.equal(calls.length, 1);
});

test("条目状态与收进收藏库：ID 被编码，结构化错误透出 code", async (t) => {
  const calls = withMockFetch(t, [
    jsonResponse(ITEM),
    jsonResponse({ item: ITEM, site_id: "s1", created: true }),
    jsonResponse({ detail: { code: "item_without_url", message: "这个条目没有可收藏的链接" } }, 422),
    new Response(null, { status: 204 }),
  ]);
  await updateFeedItem("i/1", { dismissed: true });
  assert.equal(calls[0].input, "/api/backend/feeds/items/i%2F1");
  assert.equal(calls[0].init.method, "PATCH");
  assert.equal(calls[0].init.body, JSON.stringify({ dismissed: true }));
  const saved = await saveFeedItem("i/1");
  assert.equal(saved.siteId, "s1");
  assert.equal(calls[1].input, "/api/backend/feeds/items/i%2F1/save");
  await assert.rejects(saveFeedItem("x"), (error: unknown) => {
    assert.ok(error instanceof FeedsApiError);
    assert.equal(error.code, "item_without_url");
    assert.equal(error.status, 422);
    return true;
  });
  await deleteFeed("f1");
  assert.equal(calls[3].init.method, "DELETE");
});
