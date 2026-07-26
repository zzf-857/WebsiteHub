import test from "node:test";
import assert from "node:assert/strict";

import { SECRET_MASK, type ProviderConfig, type ProviderRegistryItem } from "../lib/provider-contract.ts";
import { providerCreatePayload, providerUpdatePayload } from "../lib/provider-contract.ts";
import {
  PROVIDER_KIND_SECTIONS,
  buildProviderCreateInput,
  buildProviderUpdateInput,
  collapseDisplayName,
  createProviderDraft,
  editProviderDraft,
  effectiveHasSecret,
  hasProviderDraftError,
  providerFieldErrorFor,
  providerTestTone,
  validateProviderDraft,
  type ProviderDraft,
} from "../lib/provider-form.ts";

function registryItem(overrides: Partial<ProviderRegistryItem> = {}): ProviderRegistryItem {
  return {
    provider: "deepseek",
    label: "DeepSeek",
    kinds: ["model"],
    secretRequired: true,
    baseUrlRequired: false,
    allowsPrivateBaseUrl: false,
    applicationUrl: null,
    connectionTestSupported: false,
    defaultBaseUrl: null,
    ...overrides,
  };
}

const OLLAMA = registryItem({
  provider: "ollama",
  label: "Ollama",
  kinds: ["model", "embedding"],
  secretRequired: false,
  baseUrlRequired: true,
  allowsPrivateBaseUrl: true,
});

function storedConfig(overrides: Partial<ProviderConfig> = {}): ProviderConfig {
  return {
    id: "cfg-1",
    kind: "model",
    provider: "deepseek",
    displayName: "日常对话",
    baseUrl: "https://api.deepseek.com/v1",
    modelName: "deepseek-chat",
    enabled: true,
    hasSecret: true,
    secretMask: SECRET_MASK,
    version: 4,
    createdAt: "2026-07-01T08:00:00Z",
    updatedAt: "2026-07-02T09:30:00Z",
    ...overrides,
  };
}

function draft(overrides: Partial<ProviderDraft> = {}): ProviderDraft {
  return {
    provider: "deepseek",
    displayName: "日常对话",
    baseUrl: "",
    modelName: "deepseek-chat",
    enabled: true,
    ...overrides,
  };
}

test("三个 kind 分区齐全且顺序固定", () => {
  assert.deepEqual(
    PROVIDER_KIND_SECTIONS.map((section) => section.kind),
    ["model", "search", "embedding"],
  );
});

test("编辑时不填密钥 = 保留原密钥：请求体里完全没有 secret 键", () => {
  const config = storedConfig();
  const input = buildProviderUpdateInput({
    config,
    draft: editProviderDraft({ ...config, displayName: "改个名字" }),
    secretIntent: "keep",
    secret: "",
  });
  assert.ok(input);
  assert.equal("secret" in input, false);
  assert.equal(input.displayName, "改个名字");
  assert.equal(input.expectedVersion, 4);
  // 一路走到最终请求体也必须没有 secret，否则后端会当成清除或换新。
  assert.equal("secret" in providerUpdatePayload(input), false);
});

test("勾选换新但一个字符都没填时不会退化成清除密钥", () => {
  const config = storedConfig();
  const input = buildProviderUpdateInput({
    config,
    draft: editProviderDraft(config),
    secretIntent: "write",
    secret: "   ",
  });
  // 除密钥外没有任何改动，且空白密钥被忽略 —— 整体就是「无改动」。
  assert.equal(input, null);
});

test("显式清除密钥时产生 clear 动作", () => {
  const config = storedConfig({ enabled: false });
  const input = buildProviderUpdateInput({
    config,
    draft: editProviderDraft(config),
    secretIntent: "clear",
    secret: "",
  });
  assert.ok(input);
  assert.deepEqual(input.secret, { action: "clear" });
  assert.deepEqual(providerUpdatePayload(input).secret, { action: "clear" });
});

test("更新只提交真正变化的字段，未改动时返回 null", () => {
  const config = storedConfig();
  assert.equal(
    buildProviderUpdateInput({
      config,
      draft: editProviderDraft(config),
      secretIntent: "keep",
      secret: "",
    }),
    null,
  );

  const changed = buildProviderUpdateInput({
    config,
    draft: editProviderDraft({ ...config, enabled: false }),
    secretIntent: "keep",
    secret: "",
  });
  assert.deepEqual(changed, { expectedVersion: 4, enabled: false });
});

test("空 Base URL / 空模型名归一化成 null 而不是空字符串", () => {
  const config = storedConfig({ baseUrl: "https://api.deepseek.com/v1", modelName: "deepseek-chat" });
  const input = buildProviderUpdateInput({
    config,
    draft: draft({ baseUrl: "  ", modelName: "  ", enabled: false }),
    secretIntent: "keep",
    secret: "",
  });
  assert.ok(input);
  assert.equal(input.baseUrl, null);
  assert.equal(input.modelName, null);
});

test("Ollama 这类无需 Key 的厂商可以留空提交", () => {
  const localDraft = draft({
    provider: "ollama",
    displayName: "本地 Ollama",
    baseUrl: "http://127.0.0.1:11434",
    modelName: "qwen3:8b",
  });
  const errors = validateProviderDraft({
    kind: "model",
    definition: OLLAMA,
    draft: localDraft,
    secretIntent: "keep",
    hasStoredSecret: false,
  });
  assert.equal(hasProviderDraftError(errors), false);

  const input = buildProviderCreateInput({ kind: "model", draft: localDraft, secret: "" });
  // 没填密钥就整个不带 secret 键，后端才会按「无密钥」保存。
  assert.equal("secret" in input, false);
  assert.equal("secret" in providerCreatePayload(input), false);
});

test("需要 Key 的厂商启用前必须填 Key，填了就通过", () => {
  const definition = registryItem();
  const missing = validateProviderDraft({
    kind: "model",
    definition,
    draft: draft(),
    secretIntent: "keep",
    hasStoredSecret: false,
  });
  assert.equal(missing.secret, "启用前必须填写 API Key");

  const filled = validateProviderDraft({
    kind: "model",
    definition,
    draft: draft(),
    secretIntent: "write",
    hasStoredSecret: false,
  });
  assert.equal(hasProviderDraftError(filled), false);

  // 未启用的半成品允许先存下来（与后端 _validate_complete 的时机一致）。
  const disabled = validateProviderDraft({
    kind: "model",
    definition,
    draft: draft({ enabled: false, modelName: "" }),
    secretIntent: "keep",
    hasStoredSecret: false,
  });
  assert.equal(hasProviderDraftError(disabled), false);
});

test("清除密钥与启用互斥时给出专门的中文提示", () => {
  const errors = validateProviderDraft({
    kind: "model",
    definition: registryItem(),
    draft: draft(),
    secretIntent: "clear",
    hasStoredSecret: true,
  });
  assert.equal(errors.secret, "清除 API Key 时不能同时启用该配置");
});

test("非 Ollama 的 Base URL 必须 HTTPS 且不能指向本机或私网", () => {
  const definition = registryItem({ provider: "openai_compatible", baseUrlRequired: true });
  const check = (baseUrl: string) =>
    validateProviderDraft({
      kind: "model",
      definition,
      draft: draft({ provider: "openai_compatible", baseUrl }),
      secretIntent: "write",
      hasStoredSecret: false,
    }).baseUrl;

  assert.equal(check("http://api.example.com/v1"), "该服务商的 Base URL 必须使用 HTTPS");
  assert.equal(check("https://127.0.0.1/v1"), "该服务商不允许指向本机或局域网地址");
  assert.equal(check("https://localhost/v1"), "该服务商不允许指向本机或局域网地址");
  assert.equal(check("https://10.1.2.3/v1"), "该服务商不允许指向本机或局域网地址");
  assert.equal(check("https://192.168.1.9/v1"), "该服务商不允许指向本机或局域网地址");
  assert.equal(check("https://172.16.0.1/v1"), "该服务商不允许指向本机或局域网地址");
  assert.equal(check("https://169.254.169.254/latest"), "该服务商不允许指向本机或局域网地址");
  assert.equal(check("https://gateway.internal/v1"), "该服务商不允许指向本机或局域网地址");
  assert.equal(check("https://user:pw@api.example.com/v1"), "Base URL 不能包含用户名或密码");
  assert.equal(check("https://api.example.com/v1?key=x"), "Base URL 不能包含查询参数或片段");
  assert.equal(check("api.example.com"), "Base URL 必须是完整的 HTTP(S) 地址，例如 https://api.example.com/v1");
  assert.equal(check("https://api.example.com/v1"), undefined);
  // Ollama 允许私网与 HTTP。
  assert.equal(
    validateProviderDraft({
      kind: "model",
      definition: OLLAMA,
      draft: draft({ provider: "ollama", baseUrl: "http://192.168.1.20:11434" }),
      secretIntent: "keep",
      hasStoredSecret: false,
    }).baseUrl,
    undefined,
  );
});

test("必填 Base URL 缺失与搜索类多填模型名都会被拦下", () => {
  assert.equal(
    validateProviderDraft({
      kind: "model",
      definition: OLLAMA,
      draft: draft({ provider: "ollama", baseUrl: "" }),
      secretIntent: "keep",
      hasStoredSecret: false,
    }).baseUrl,
    "该服务商必须填写 Base URL",
  );

  assert.equal(
    validateProviderDraft({
      kind: "search",
      definition: registryItem({ provider: "tavily", label: "Tavily", kinds: ["search"] }),
      draft: draft({ provider: "tavily", modelName: "不该填" }),
      secretIntent: "write",
      hasStoredSecret: false,
    }).modelName,
    "搜索服务不需要填写模型名称",
  );
});

test("未选服务商时只报服务商这一个错", () => {
  assert.deepEqual(
    validateProviderDraft({
      kind: "model",
      definition: null,
      draft: createProviderDraft(),
      secretIntent: "keep",
      hasStoredSecret: false,
    }),
    { provider: "请先选择服务商" },
  );
});

test("配置名称按后端规则折叠空白后再判长度", () => {
  assert.equal(collapseDisplayName("  日常   对话 \n"), "日常 对话");
  assert.equal(
    validateProviderDraft({
      kind: "model",
      definition: registryItem(),
      draft: draft({ displayName: "   " }),
      secretIntent: "write",
      hasStoredSecret: false,
    }).displayName,
    "请填写配置名称",
  );
  assert.equal(
    validateProviderDraft({
      kind: "model",
      definition: registryItem(),
      draft: draft({ displayName: "名".repeat(81) }),
      secretIntent: "write",
      hasStoredSecret: false,
    }).displayName,
    "配置名称不能超过 80 个字符",
  );
  // 折叠后的名称才是比对基准，避免「只多敲了个空格」被当成一次改动发出去。
  const config = storedConfig({ displayName: "日常 对话" });
  assert.equal(
    buildProviderUpdateInput({
      config,
      draft: editProviderDraft({ ...config, displayName: "  日常   对话  " }),
      secretIntent: "keep",
      secret: "",
    }),
    null,
  );
});

test("创建请求体按 kind 与草稿原样构造", () => {
  const input = buildProviderCreateInput({
    kind: "model",
    draft: draft({ baseUrl: " https://api.deepseek.com/v1 ", modelName: " deepseek-chat " }),
    secret: "  sk-test-123  ",
  });
  assert.deepEqual(input, {
    kind: "model",
    provider: "deepseek",
    displayName: "日常对话",
    baseUrl: "https://api.deepseek.com/v1",
    modelName: "deepseek-chat",
    secret: "sk-test-123",
    enabled: true,
  });
  const payload = providerCreatePayload(input);
  assert.deepEqual(payload.secret, { action: "write", value: "sk-test-123" });
  assert.equal("expected_version" in payload, false);
});

test("effectiveHasSecret 三态语义", () => {
  assert.equal(effectiveHasSecret("keep", true), true);
  assert.equal(effectiveHasSecret("keep", false), false);
  assert.equal(effectiveHasSecret("write", false), true);
  assert.equal(effectiveHasSecret("clear", true), false);
});

test("新建草稿默认勾选启用，并按注册表预填厂商官方地址", () => {
  // 没选厂商：全空。
  assert.deepEqual(createProviderDraft(), {
    provider: "",
    displayName: "",
    baseUrl: "",
    modelName: "",
    enabled: true,
  });
  // 选了有官方地址的厂商：Base URL 和配置名都不用用户自己敲。
  assert.deepEqual(
    createProviderDraft(
      registryItem({ provider: "openai", label: "OpenAI", defaultBaseUrl: "https://api.openai.com/v1" }),
    ),
    {
      provider: "openai",
      displayName: "OpenAI",
      baseUrl: "https://api.openai.com/v1",
      modelName: "",
      enabled: true,
    },
  );
  // 没有官方地址的厂商（Ollama / OpenAI-compatible）仍然要用户自己填。
  assert.equal(createProviderDraft(OLLAMA).baseUrl, "");
  assert.deepEqual(editProviderDraft(storedConfig({ baseUrl: null, modelName: null })), {
    provider: "deepseek",
    displayName: "日常对话",
    baseUrl: "",
    modelName: "",
    enabled: true,
  });
});

test("后端 error code 能落到具体字段，未知 code 交回表单级", () => {
  assert.deepEqual(providerFieldErrorFor("duplicate_provider_name", "同类型中已存在同名配置"), {
    field: "displayName",
    message: "同类型中已存在同名配置",
  });
  assert.equal(providerFieldErrorFor("insecure_base_url", "")?.field, "baseUrl");
  assert.equal(providerFieldErrorFor("insecure_base_url", "")?.message, "该服务商的 Base URL 必须使用 HTTPS");
  assert.equal(providerFieldErrorFor("provider_config_incomplete", "配置尚不完整"), null);
  assert.equal(providerFieldErrorFor(undefined, "未知错误"), null);
});

test("连接测试状态映射到 CSS 的 data-status 取值", () => {
  assert.equal(providerTestTone("ok"), "ok");
  assert.equal(providerTestTone("error"), "error");
  assert.equal(providerTestTone("rate_limited"), "rate-limited");
  // 后端当前恒返回 unsupported：走中性样式，不能被渲染成成功。
  assert.equal(providerTestTone("unsupported"), "neutral");
});

test("测试连接不要求模型名与配置名——那正是这份列表要帮用户找到的东西", () => {
  const definition = registryItem();
  const blank = draft({ displayName: "", modelName: "" });

  // 保存路径仍然要求配齐。
  const saving = validateProviderDraft({
    kind: "model",
    definition,
    draft: blank,
    secretIntent: "write",
    hasStoredSecret: false,
  });
  assert.equal(saving.displayName, "请填写配置名称");
  assert.equal(saving.modelName, "启用前必须填写模型名称");

  // 测试路径只要求「够得着厂商」的字段。
  const testing = validateProviderDraft({
    kind: "model",
    definition,
    draft: blank,
    secretIntent: "write",
    hasStoredSecret: false,
    mode: "test",
  });
  assert.equal(hasProviderDraftError(testing), false);
});

test("测试连接仍然要求 Key 与合法 Base URL", () => {
  const noKey = validateProviderDraft({
    kind: "model",
    definition: registryItem(),
    draft: draft({ displayName: "", modelName: "" }),
    secretIntent: "keep",
    hasStoredSecret: false,
    mode: "test",
  });
  assert.equal(noKey.secret, "测试连接前必须填写 API Key");

  // 编辑一个已存密钥的配置时，不重填也能测。
  const stored = validateProviderDraft({
    kind: "model",
    definition: registryItem(),
    draft: draft({ displayName: "", modelName: "" }),
    secretIntent: "keep",
    hasStoredSecret: true,
    mode: "test",
  });
  assert.equal(hasProviderDraftError(stored), false);

  const unsafe = validateProviderDraft({
    kind: "model",
    definition: registryItem({ provider: "openai_compatible", baseUrlRequired: true }),
    draft: draft({ provider: "openai_compatible", baseUrl: "https://127.0.0.1/v1" }),
    secretIntent: "write",
    hasStoredSecret: false,
    mode: "test",
  });
  assert.equal(unsafe.baseUrl, "该服务商不允许指向本机或局域网地址");
});
