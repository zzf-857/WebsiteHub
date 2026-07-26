import test from "node:test";
import assert from "node:assert/strict";

import {
  ProviderContractError,
  SECRET_MASK,
  normalizeProviderConfig,
  normalizeProviderRegistry,
  providerCreatePayload,
  providerErrorDetails,
  providerTestPayload,
  providerUpdatePayload,
} from "../lib/provider-contract.ts";

function sampleConfigResponse(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "cfg-1",
    kind: "model",
    provider: "deepseek",
    display_name: "DeepSeek 主力",
    base_url: "https://api.deepseek.com/v1",
    model_name: "deepseek-chat",
    enabled: true,
    has_secret: true,
    secret_mask: SECRET_MASK,
    version: 3,
    created_at: "2026-07-01T08:00:00Z",
    updated_at: "2026-07-02T09:30:00Z",
    ...overrides,
  };
}

test("创建请求体的 secret action 是 write", () => {
  const payload = providerCreatePayload({
    kind: "model",
    provider: "openai",
    displayName: "OpenAI",
    secret: "sk-test-123",
  });
  assert.deepEqual(payload.secret, { action: "write", value: "sk-test-123" });
  // 创建没有乐观锁语义，请求体里绝不能出现 expected_version。
  assert.equal("expected_version" in payload, false);
});

test("更新请求体换新密钥时 secret action 是 replace", () => {
  const payload = providerUpdatePayload({
    expectedVersion: 3,
    secret: { action: "replace", value: "sk-new-456" },
  });
  assert.equal(payload.expected_version, 3);
  assert.deepEqual(payload.secret, { action: "replace", value: "sk-new-456" });
});

test("更新请求体清除密钥时 secret action 是 clear 且不带 value", () => {
  const payload = providerUpdatePayload({
    expectedVersion: 3,
    secret: { action: "clear" },
  });
  assert.deepEqual(payload.secret, { action: "clear" });
});

test("测试未保存配置时 secret action 是 test", () => {
  const payload = providerTestPayload({
    kind: "search",
    provider: "tavily",
    secret: "tvly-test-789",
  });
  assert.deepEqual(payload, {
    kind: "search",
    provider: "tavily",
    secret: { action: "test", value: "tvly-test-789" },
  });
});

test("更新不传 secret 时请求体里完全没有 secret 键", () => {
  const payload = providerUpdatePayload({
    expectedVersion: 2,
    displayName: "改个名字",
  });
  // 「不携带 secret 键」本身就是「保留原密钥」的信号，出现 secret: undefined/null 都算错。
  assert.equal("secret" in payload, false);
  assert.deepEqual(payload, { expected_version: 2, display_name: "改个名字" });
});

test("测试请求体的互斥校验在前端就抛错", () => {
  // 只给 configId 缺 expectedVersion
  assert.throws(() => providerTestPayload({ configId: "cfg-1" }), ProviderContractError);
  // 给了 configId 又混入临时参数
  assert.throws(
    () => providerTestPayload({ configId: "cfg-1", expectedVersion: 2, provider: "openai" }),
    ProviderContractError,
  );
  // 未保存形态却携带 expectedVersion
  assert.throws(
    () => providerTestPayload({ kind: "model", provider: "openai", expectedVersion: 2 }),
    ProviderContractError,
  );
  // 未保存形态缺 kind/provider
  assert.throws(() => providerTestPayload({ kind: "model" }), ProviderContractError);
  // 合法的已保存形态只包含 config_id 与 expected_version
  assert.deepEqual(providerTestPayload({ configId: "cfg-1", expectedVersion: 2 }), {
    config_id: "cfg-1",
    expected_version: 2,
  });
});

test("归一化把 snake_case 转成 camelCase", () => {
  const config = normalizeProviderConfig(sampleConfigResponse());
  assert.equal(config.displayName, "DeepSeek 主力");
  assert.equal(config.baseUrl, "https://api.deepseek.com/v1");
  assert.equal(config.modelName, "deepseek-chat");
  assert.equal(config.hasSecret, true);
  assert.equal(config.secretMask, SECRET_MASK);
  assert.equal(config.createdAt, "2026-07-01T08:00:00Z");
  assert.equal(config.updatedAt, "2026-07-02T09:30:00Z");

  const registry = normalizeProviderRegistry({
    items: [
      {
        provider: "tavily",
        label: "Tavily",
        kinds: ["search"],
        secret_required: true,
        base_url_required: false,
        allows_private_base_url: false,
        application_url: "https://app.tavily.com",
        connection_test_supported: false,
      },
    ],
  });
  assert.equal(registry.length, 1);
  assert.deepEqual(registry[0], {
    provider: "tavily",
    label: "Tavily",
    kinds: ["search"],
    secretRequired: true,
    baseUrlRequired: false,
    allowsPrivateBaseUrl: false,
    applicationUrl: "https://app.tavily.com",
    connectionTestSupported: false,
  });
});

test("响应里出现明文密钥字段时归一化直接抛错", () => {
  // 后端契约只允许返回掩码；这些键的出现意味着后端把明文漏出来了，前端必须拒收。
  assert.throws(
    () => normalizeProviderConfig(sampleConfigResponse({ api_key: "sk-leaked" })),
    ProviderContractError,
  );
  assert.throws(
    () => normalizeProviderConfig(sampleConfigResponse({ apiKey: "sk-leaked" })),
    ProviderContractError,
  );
  assert.throws(
    () => normalizeProviderConfig(sampleConfigResponse({ secret: "sk-leaked" })),
    ProviderContractError,
  );
  assert.throws(
    () => normalizeProviderConfig(sampleConfigResponse({ token: "sk-leaked" })),
    ProviderContractError,
  );
  // 合法的掩码字段不受影响
  assert.equal(normalizeProviderConfig(sampleConfigResponse()).secretMask, SECRET_MASK);
});

test("409 无后端文案时给出中文版本冲突兜底", () => {
  const details = providerErrorDetails(409, { detail: { code: "version_conflict" } });
  assert.equal(details.code, "version_conflict");
  assert.equal(details.message, "配置已被更新，请刷新后重试");
});

test("错误详情能解析 detail.code 与 detail.message", () => {
  const details = providerErrorDetails(422, {
    detail: { code: "provider_base_url_invalid", message: "base_url 不允许指向私网地址" },
  });
  assert.equal(details.code, "provider_base_url_invalid");
  assert.equal(details.message, "base_url 不允许指向私网地址");
});
