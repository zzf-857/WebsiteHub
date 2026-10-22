import test from "node:test";
import assert from "node:assert/strict";

import {
  AgentAccessContractError,
  TOKEN_PLACEHOLDER,
  agentAccessLabel,
  agentErrorDetails,
  agentTokenCreatePayload,
  agentTokenDisplayPrefix,
  agentToolLabel,
  formatAgentMoment,
  genericMcpConfigSnippet,
  hermesConfigSnippet,
  listedAgentToken,
  mcpEndpointUrl,
  normalizeAgentActivityList,
  normalizeAgentTokenList,
  normalizeCreatedAgentToken,
  utcTimestamp,
} from "../lib/agent-access-contract.ts";

function tokenResponse(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "tok-1",
    name: "Hermes",
    prefix: "whk_AbCd",
    scopes: ["read", "write"],
    created_at: "2026-10-22T06:00:00Z",
    last_used_at: null,
    ...overrides,
  };
}

test("令牌列表：解析展示字段，write 隐含 read，缺省上限为 20", () => {
  const list = normalizeAgentTokenList({
    items: [tokenResponse(), tokenResponse({ id: "tok-2", scopes: ["read"], last_used_at: "2026-10-22T07:00:00" })],
  });
  assert.equal(list.maxActive, 20);
  assert.deepEqual(list.items[0], {
    id: "tok-1",
    name: "Hermes",
    prefix: "whk_AbCd",
    scopes: ["read", "write"],
    canWrite: true,
    createdAt: "2026-10-22T06:00:00Z",
    lastUsedAt: null,
  });
  assert.equal(list.items[1].canWrite, false);
  // SQLite 不带时区：按 UTC 补 Z。
  assert.equal(list.items[1].lastUsedAt, "2026-10-22T07:00:00Z");

  const implied = normalizeAgentTokenList({ items: [tokenResponse({ scopes: ["write"] })], max_active: 5 });
  assert.deepEqual(implied.items[0].scopes, ["read", "write"]);
  assert.equal(implied.maxActive, 5);
  assert.equal(agentAccessLabel(implied.items[0]), "读写");
  assert.equal(agentAccessLabel(list.items[1]), "只读");
  assert.equal(agentTokenDisplayPrefix(list.items[0]), "whk_AbCd…");
});

test("令牌列表：未知权限、空权限、缺字段一律拒绝", () => {
  for (const bad of [
    tokenResponse({ scopes: ["admin"] }),
    tokenResponse({ scopes: [] }),
    tokenResponse({ prefix: "" }),
    tokenResponse({ created_at: "not a date" }),
    tokenResponse({ name: "x".repeat(41) }),
  ]) {
    assert.throws(() => normalizeAgentTokenList({ items: [bad] }), AgentAccessContractError);
  }
  assert.throws(() => normalizeAgentTokenList({ items: "nope" }), AgentAccessContractError);
});

test("创建响应必须带 whk_ 开头的明文令牌", () => {
  const created = normalizeCreatedAgentToken({ ...tokenResponse(), token: "whk_secretvalue" });
  assert.equal(created.token, "whk_secretvalue");
  assert.equal(created.canWrite, true);
  const listed = listedAgentToken(created);
  assert.equal("token" in listed, false);
  assert.equal(JSON.stringify(listed).includes("whk_secretvalue"), false);
  assert.throws(() => normalizeCreatedAgentToken(tokenResponse()), AgentAccessContractError);
  assert.throws(
    () => normalizeCreatedAgentToken({ ...tokenResponse(), token: "secret" }),
    AgentAccessContractError,
  );
});

test("创建请求体：名称规整、长度按字计、权限映射为 scopes", () => {
  assert.deepEqual(agentTokenCreatePayload({ name: "  Hermes   主力 ", access: "read-write" }), {
    name: "Hermes 主力",
    scopes: ["read", "write"],
  });
  assert.deepEqual(agentTokenCreatePayload({ name: "grokbot", access: "read" }), {
    name: "grokbot",
    scopes: ["read"],
  });
  assert.equal(agentTokenCreatePayload({ name: "中".repeat(40), access: "read" }).name.length, 40);
  assert.throws(() => agentTokenCreatePayload({ name: "   ", access: "read" }), AgentAccessContractError);
  assert.throws(
    () => agentTokenCreatePayload({ name: "中".repeat(41), access: "read" }),
    AgentAccessContractError,
  );
  assert.throws(
    () => agentTokenCreatePayload({ name: "x", access: "admin" as never }),
    AgentAccessContractError,
  );
});

test("活动列表：解析时间、令牌名、工具与目标", () => {
  const items = normalizeAgentActivityList({
    items: [
      {
        id: "a1",
        created_at: "2026-10-22T06:00:00.123456",
        token_id: "tok-1",
        token_name: "Hermes",
        tool: "save_resource",
        site_id: "site-1",
        summary: "收藏 https://example.com/",
      },
      {
        id: "a2",
        created_at: "2026-10-22T05:00:00+00:00",
        token_id: "tok-1",
        token_name: "Hermes",
        tool: "future_tool",
        site_id: null,
        summary: "其他",
      },
    ],
  });
  assert.equal(items[0].createdAt, "2026-10-22T06:00:00.123456Z");
  assert.equal(items[0].siteId, "site-1");
  assert.equal(items[1].createdAt, "2026-10-22T05:00:00+00:00");
  assert.equal(items[1].siteId, null);
  assert.equal(agentToolLabel(items[0].tool), "收藏");
  assert.equal(agentToolLabel("update_resource"), "整理");
  assert.equal(agentToolLabel("mark_resource"), "标记");
  assert.equal(agentToolLabel(items[1].tool), "future_tool");
  assert.throws(
    () => normalizeAgentActivityList({ items: [{ id: "a3", created_at: "x" }] }),
    AgentAccessContractError,
  );
});

test("utcTimestamp 只给不带时区的值补 Z", () => {
  assert.equal(utcTimestamp("2026-10-22T06:00:00", "t"), "2026-10-22T06:00:00Z");
  assert.equal(utcTimestamp("2026-10-22T06:00:00Z", "t"), "2026-10-22T06:00:00Z");
  assert.equal(utcTimestamp("2026-10-22T14:00:00+08:00", "t"), "2026-10-22T14:00:00+08:00");
  assert.throws(() => utcTimestamp("", "t"), AgentAccessContractError);
});

test("配置片段：Hermes YAML 与通用 JSON，未创建令牌时用占位符", () => {
  const url = mcpEndpointUrl("http://webhub.tail1234.ts.net:3100/");
  assert.equal(url, "http://webhub.tail1234.ts.net:3100/api/backend/mcp");

  assert.equal(
    hermesConfigSnippet({ url, token: "whk_abc" }),
    [
      "mcp_servers:",
      "  webhub:",
      '    url: "http://webhub.tail1234.ts.net:3100/api/backend/mcp"',
      "    headers:",
      '      Authorization: "Bearer whk_abc"',
      "",
    ].join("\n"),
  );
  assert.match(hermesConfigSnippet({ url, token: null }), new RegExp(`Bearer ${TOKEN_PLACEHOLDER}`));
  assert.match(hermesConfigSnippet({ url: 'http://a"b', token: null }), /url: "http:\/\/a\\"b"/);

  const generic = JSON.parse(genericMcpConfigSnippet({ url, token: "whk_abc" }));
  assert.deepEqual(generic, {
    mcpServers: {
      webhub: {
        type: "http",
        url,
        headers: { Authorization: "Bearer whk_abc" },
      },
    },
  });
  assert.match(genericMcpConfigSnippet({ url }), /Bearer <令牌>/);
});

test("错误详情：结构化 detail、字符串、422 数组与兜底文案", () => {
  assert.deepEqual(
    agentErrorDetails(409, { detail: { code: "token_limit", message: "最多同时保留 20 个有效令牌" } }),
    { code: "token_limit", message: "最多同时保留 20 个有效令牌" },
  );
  assert.deepEqual(agentErrorDetails(401, { detail: "请先登录" }), { message: "请先登录" });
  assert.deepEqual(
    agentErrorDetails(422, { detail: [{ msg: "String should have at most 40 characters" }] }),
    { message: "String should have at most 40 characters" },
  );
  assert.equal(agentErrorDetails(404, null).message, "令牌不存在或已被吊销");
  assert.equal(agentErrorDetails(503, "boom").message, "服务暂时不可用，请稍后重试");
});

test("时间展示：null 显示从未使用", () => {
  assert.equal(formatAgentMoment(null), "从未使用");
  assert.equal(formatAgentMoment("garbage"), "garbage");
  assert.match(formatAgentMoment("2026-10-22T06:00:00Z"), /2026/u);
});
