import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";

import {
  AgentAccessApiError,
  createAgentToken,
  listAgentActivity,
  listAgentTokens,
  revokeAgentToken,
} from "../lib/agent-access-client.ts";
import { AgentAccessContractError } from "../lib/agent-access-contract.ts";

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

const TOKEN = {
  id: "tok-1",
  name: "Hermes",
  prefix: "whk_AbCd",
  scopes: ["read", "write"],
  created_at: "2026-10-22T06:00:00Z",
  last_used_at: null,
};

test("列出令牌：走同源代理、带 Cookie、不读缓存", async (t) => {
  const calls = withMockFetch(t, [jsonResponse({ items: [TOKEN], max_active: 20 })]);
  const list = await listAgentTokens();
  assert.equal(calls[0].input, "/api/backend/agent-tokens");
  assert.equal(calls[0].init.credentials, "include");
  assert.equal(calls[0].init.cache, "no-store");
  assert.equal(calls[0].init.method, undefined);
  assert.equal(list.items[0].name, "Hermes");
});

test("创建令牌：POST JSON 体，返回一次性明文", async (t) => {
  const calls = withMockFetch(t, [jsonResponse({ ...TOKEN, token: "whk_plaintext" }, 201)]);
  const created = await createAgentToken({ name: " Hermes ", access: "read-write" });
  assert.equal(calls[0].input, "/api/backend/agent-tokens");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(String(calls[0].init.body)), {
    name: "Hermes",
    scopes: ["read", "write"],
  });
  assert.deepEqual(calls[0].init.headers, { "Content-Type": "application/json" });
  assert.equal(created.token, "whk_plaintext");
});

test("创建令牌：本地校验失败时不发请求", async (t) => {
  const calls = withMockFetch(t, []);
  await assert.rejects(createAgentToken({ name: "  ", access: "read" }), AgentAccessContractError);
  assert.equal(calls.length, 0);
});

test("创建令牌：上限 409 映射为带 code 的错误", async (t) => {
  withMockFetch(t, [
    jsonResponse({ detail: { code: "token_limit", message: "最多同时保留 20 个有效令牌" } }, 409),
  ]);
  await assert.rejects(createAgentToken({ name: "x", access: "read" }), (error: unknown) => {
    assert.ok(error instanceof AgentAccessApiError);
    assert.equal(error.status, 409);
    assert.equal(error.code, "token_limit");
    assert.equal(error.message, "最多同时保留 20 个有效令牌");
    return true;
  });
});

test("吊销令牌：DELETE 编码后的 ID，204 无响应体", async (t) => {
  const calls = withMockFetch(t, [new Response(null, { status: 204 })]);
  await revokeAgentToken(" tok/1 ");
  assert.equal(calls[0].input, "/api/backend/agent-tokens/tok%2F1");
  assert.equal(calls[0].init.method, "DELETE");
  assert.equal(calls[0].init.body, undefined);
  await assert.rejects(revokeAgentToken("  "), TypeError);
});

test("活动列表：limit 夹在 1–200 之间", async (t) => {
  const calls = withMockFetch(t, [
    jsonResponse({ items: [] }),
    jsonResponse({ items: [] }),
    jsonResponse({
      items: [
        {
          id: "a1",
          created_at: "2026-10-22T06:00:00",
          token_id: "tok-1",
          token_name: "Hermes",
          tool: "save_resource",
          site_id: "s1",
          summary: "收藏 https://example.com/",
        },
      ],
    }),
  ]);
  await listAgentActivity(5000);
  await listAgentActivity(0);
  const items = await listAgentActivity();
  assert.equal(calls[0].input, "/api/backend/agent-activity?limit=200");
  assert.equal(calls[1].input, "/api/backend/agent-activity?limit=1");
  assert.equal(calls[2].input, "/api/backend/agent-activity?limit=50");
  assert.equal(items[0].createdAt, "2026-10-22T06:00:00Z");
});

test("非 JSON 错误响应退回状态码兜底文案", async (t) => {
  withMockFetch(t, [new Response("<html>bad gateway</html>", { status: 502 })]);
  await assert.rejects(listAgentTokens(), (error: unknown) => {
    assert.ok(error instanceof AgentAccessApiError);
    assert.equal(error.status, 502);
    assert.equal(error.message, "服务暂时不可用，请稍后重试");
    return true;
  });
});
