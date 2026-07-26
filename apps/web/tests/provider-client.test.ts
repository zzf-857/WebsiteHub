import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";

import {
  ProviderApiError,
  createProviderConfig,
  deleteProviderConfig,
  listProviderRegistry,
  testProviderConnection,
  updateProviderConfig,
} from "../lib/provider-client.ts";
import { ProviderContractError, SECRET_MASK } from "../lib/provider-contract.ts";

type FetchCall = {
  input: string;
  init: RequestInit;
};

function jsonResponse(payload: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function sampleConfigResponse(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "cfg-1",
    kind: "model",
    provider: "openai",
    display_name: "OpenAI",
    base_url: null,
    model_name: "gpt-4o-mini",
    enabled: true,
    has_secret: true,
    secret_mask: SECRET_MASK,
    version: 3,
    created_at: "2026-07-01T08:00:00Z",
    updated_at: "2026-07-02T09:30:00Z",
    ...overrides,
  };
}

// 替换全局 fetch 并记录每次调用；测试结束自动还原，避免污染其他用例。
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

test("delete 的 expected_version 走查询参数而不是请求体", async (t) => {
  const calls = withMockFetch(t, [jsonResponse({ message: "已删除", config_id: "cfg-1" })]);
  await deleteProviderConfig("cfg-1", 3);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].input, "/api/backend/providers/cfg-1?expected_version=3");
  assert.equal(calls[0].init.method, "DELETE");
  assert.equal(calls[0].init.body, undefined);
});

test("含斜杠的 id 会做 encodeURIComponent", async (t) => {
  const calls = withMockFetch(t, [jsonResponse(sampleConfigResponse())]);
  await updateProviderConfig("team/openai", { expectedVersion: 3, displayName: "改名" });
  assert.equal(calls[0].input, "/api/backend/providers/team%2Fopenai");
  assert.equal(calls[0].init.method, "PATCH");
});

test("请求统一携带 credentials include 与 cache no-store", async (t) => {
  const calls = withMockFetch(t, [
    jsonResponse({ items: [] }),
    jsonResponse(sampleConfigResponse(), 201),
  ]);

  await listProviderRegistry();
  const created = await createProviderConfig({
    kind: "model",
    provider: "openai",
    displayName: "OpenAI",
    secret: "sk-test-123",
  });

  for (const call of calls) {
    assert.equal(call.init.credentials, "include");
    assert.equal(call.init.cache, "no-store");
  }
  // 无 body 的 GET 不应附加 Content-Type；带 body 的 POST 必须是 JSON。
  assert.equal(calls[0].init.headers, undefined);
  assert.deepEqual(calls[1].init.headers, { "Content-Type": "application/json" });
  assert.equal(created.displayName, "OpenAI");
});

test("429 响应能解析出 retryAfterSeconds", async (t) => {
  withMockFetch(t, [
    jsonResponse(
      { detail: { code: "provider_test_rate_limited", message: "测试过于频繁" } },
      429,
      { "retry-after": "17" },
    ),
  ]);
  await assert.rejects(
    testProviderConnection({ kind: "model", provider: "openai", secret: "sk-test-123" }),
    (error: unknown) => {
      assert.ok(error instanceof ProviderApiError);
      assert.equal(error.status, 429);
      assert.equal(error.code, "provider_test_rate_limited");
      assert.equal(error.retryAfterSeconds, 17);
      return true;
    },
  );
});

test("409 冲突在无后端文案时抛出中文提示", async (t) => {
  withMockFetch(t, [jsonResponse({ detail: { code: "version_conflict" } }, 409)]);
  await assert.rejects(
    updateProviderConfig("cfg-1", { expectedVersion: 2, displayName: "改名" }),
    (error: unknown) => {
      assert.ok(error instanceof ProviderApiError);
      assert.equal(error.status, 409);
      assert.equal(error.message, "配置已被更新，请刷新后重试");
      return true;
    },
  );
});

test("test payload 非法组合在前端抛错且不发出 fetch", async (t) => {
  const calls = withMockFetch(t, []);
  // configId 缺 expectedVersion：应在本地被拦下，网络层完全不动。
  await assert.rejects(testProviderConnection({ configId: "cfg-1" }), ProviderContractError);
  await assert.rejects(
    testProviderConnection({ configId: "cfg-1", expectedVersion: 2, provider: "openai" }),
    ProviderContractError,
  );
  assert.equal(calls.length, 0);
});

test("响应携带明文密钥字段时客户端拒收", async (t) => {
  withMockFetch(t, [jsonResponse(sampleConfigResponse({ api_key: "sk-leaked" }))]);
  await assert.rejects(
    updateProviderConfig("cfg-1", { expectedVersion: 3, displayName: "改名" }),
    ProviderContractError,
  );
});
