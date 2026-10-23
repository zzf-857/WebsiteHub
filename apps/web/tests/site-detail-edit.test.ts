import assert from "node:assert/strict";
import test from "node:test";

import { listLibrarySiteSelection, listLibrarySites, updateLibrarySite } from "../lib/library-client.ts";
import { normalizeLibrarySite, type LibraryTag } from "../lib/library-contract.ts";
import { formatPushedAgo, repoPanelModel } from "../lib/repo-panel.ts";
import {
  categoryUpdateInput,
  exactTagOption,
  filterTagOptions,
  MAX_TAG_PICKER_OPTIONS,
  sortCategoryOptions,
  tagCreateCandidate,
  tagsUpdateInput,
  toggleTagSelection,
} from "../lib/site-taxonomy-edit.ts";
import { siteFixture } from "./discover-fixtures.ts";

const DAY = 86_400_000;
const NOW = Date.parse("2026-10-22T12:00:00Z");

const tags: LibraryTag[] = [
  { id: "t-agent", name: "Agent", siteCount: 4 },
  { id: "t-browser", name: "浏览器自动化", siteCount: 2 },
  { id: "t-open", name: "开源", siteCount: 9 },
  { id: "t-strasse", name: "Straße", siteCount: 1 },
];

const apiSite = {
  id: "site-1",
  name: "browser-use",
  original_url: "https://github.com/browser-use/browser-use",
  identity_url: "https://github.com/browser-use/browser-use",
  summary: null,
  description: null,
  favicon_url: null,
  preview_url: null,
  category: { id: "c-dev", name: "开发", is_default: false, icon: "Code" },
  tags: [{ id: "t-agent", name: "Agent" }],
  pinned: false,
  source: "agent",
  analysis_status: "complete",
  analysis_phase: null,
  version: 7,
  created_at: "2026-09-09T10:00:00",
  updated_at: "2026-09-10T10:00:00",
};

function captureFetch(context: { after: (fn: () => void) => void }, body: unknown = apiSite) {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  const requests: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ url: String(input), init });
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  return requests;
}

// ── 仓库面板 ────────────────────────────────────────────────────────────

test("the repo panel renders nothing without GitHub data", () => {
  assert.equal(repoPanelModel({}, NOW), null);
  assert.equal(repoPanelModel({ host: "github.com", owner: "a", repo: "b" }, NOW), null, "坐标不是统计");
  assert.equal(repoPanelModel({ github: null }, NOW), null);
  assert.equal(repoPanelModel({ github: ["x"] }, NOW), null);
  assert.equal(repoPanelModel({ github: { etag: "W/1", fetched_at: "2026-10-01T00:00:00Z" } }, NOW), null);
});

test("the repo panel maps every GitHub field", () => {
  const model = repoPanelModel(
    {
      host: "github.com",
      github: {
        stars: 51_234,
        forks: 1_234,
        language: " Python ",
        topics: ["agent", "browser", "Agent", "", 3, "automation"],
        license: "MIT",
        pushed_at: "2026-10-19T08:00:00Z",
        archived: true,
        etag: "W/abc",
      },
    },
    NOW,
  );
  assert.deepEqual(model, {
    stars: "5.1 万",
    forks: "1,234",
    language: "Python",
    topics: ["agent", "browser", "automation"],
    license: "MIT",
    pushed: "3 天前推送",
    pushedAt: "2026-10-19T08:00:00Z",
    archived: true,
  });
});

test("repo panel fields degrade one by one", () => {
  const partial = repoPanelModel({ github: { stars: 0, license: { spdx_id: "NOASSERTION", name: "Other" } } }, NOW);
  assert.equal(partial?.stars, "0");
  assert.equal(partial?.forks, null);
  assert.equal(partial?.license, "Other", "NOASSERTION 退回许可证名称");
  assert.equal(partial?.archived, false);
  assert.deepEqual(partial?.topics, []);

  const spdx = repoPanelModel({ github: { license: { spdx_id: "Apache-2.0", name: "Apache License 2.0" } } }, NOW);
  assert.equal(spdx?.license, "Apache-2.0");

  const invalid = repoPanelModel({ github: { stars: -1, forks: Number.NaN, pushed_at: "not a date", language: 3 } }, NOW);
  assert.equal(invalid, null, "全是非法值时整块不渲染");

  const onlyArchived = repoPanelModel({ github: { archived: true } }, NOW);
  assert.equal(onlyArchived?.archived, true);
  assert.equal(onlyArchived?.pushed, null);

  const many = repoPanelModel({ github: { topics: Array.from({ length: 30 }, (_, i) => `t${i}`) } }, NOW);
  assert.equal(many?.topics.length, 12);
});

test("pushed time reads as whole days, in UTC, never negative", () => {
  assert.equal(formatPushedAgo("2026-10-22T11:00:00Z", NOW), "今天推送");
  assert.equal(formatPushedAgo("2026-10-21T11:59:00Z", NOW), "1 天前推送");
  // SQLite 风格、不带时区的时间按 UTC 解析
  assert.equal(formatPushedAgo("2026-10-12T12:00:00", NOW), "10 天前推送");
  assert.equal(formatPushedAgo("2026-10-30T00:00:00Z", NOW), "今天推送", "时钟偏差不出现负数");
  assert.equal(formatPushedAgo("garbage", NOW), null);
  assert.equal(formatPushedAgo("2026-10-20T00:00:00Z", Number.NaN), null);
  assert.ok(NOW - Date.parse("2026-10-12T12:00:00Z") === 10 * DAY);
});

// ── 分类与标签就地编辑 ──────────────────────────────────────────────────

test("tag options filter by the backend's normalized name and list selected tags first", () => {
  const selected = new Set(["t-open"]);
  assert.deepEqual(filterTagOptions(tags, "", selected).map((tag) => tag.id), [
    "t-open",
    "t-agent",
    "t-browser",
    "t-strasse",
  ]);
  assert.deepEqual(filterTagOptions(tags, "  AGE ", new Set()).map((tag) => tag.id), ["t-agent"]);
  assert.deepEqual(filterTagOptions(tags, "strasse", new Set()).map((tag) => tag.id), ["t-strasse"], "ß 按 casefold 比较");
  const many = Array.from({ length: 100 }, (_, index) => ({ id: `t${index}`, name: `tag ${index}` }));
  assert.equal(filterTagOptions(many, "tag", new Set()).length, MAX_TAG_PICKER_OPTIONS);
});

test("a new tag can be created only when no equivalent tag exists", () => {
  assert.equal(tagCreateCandidate(tags, "  Hermes   插件 "), "Hermes 插件", "按后端规则规范化空白");
  assert.equal(tagCreateCandidate(tags, "agent"), null, "大小写不同也算同名");
  assert.equal(tagCreateCandidate(tags, "STRASSE"), null);
  assert.equal(tagCreateCandidate(tags, "ＡＧＥＮＴ"), null, "全角按 NFKC 归一");
  assert.equal(tagCreateCandidate(tags, "   "), null);
  assert.equal(tagCreateCandidate(tags, "x".repeat(41)), null, "超过 40 个字符");
  assert.equal(tagCreateCandidate(tags, "x".repeat(40)), "x".repeat(40));
});

test("Enter picks the existing tag under the same normalization that blocks creating it", () => {
  for (const query of ["agent", "  AGENT ", "ＡＧＥＮＴ", "STRASSE"]) {
    assert.equal(tagCreateCandidate(tags, query), null, query);
    assert.ok(exactTagOption(tags, query), `回车应勾选已有标签：${query}`);
  }
  assert.equal(exactTagOption(tags, "ＡＧＥＮＴ")?.id, "t-agent");
  assert.equal(exactTagOption(tags, "age"), null, "只认完整同名，不认前缀");
  assert.equal(exactTagOption(tags, "   "), null);
});

test("toggling keeps the user's order", () => {
  assert.deepEqual(toggleTagSelection(["a", "b"], "c"), ["a", "b", "c"]);
  assert.deepEqual(toggleTagSelection(["a", "b", "c"], "b"), ["a", "c"]);
});

test("inline edits only produce a request when something changed", () => {
  const site = siteFixture({
    version: 7,
    category: { id: "c-dev", name: "开发", isDefault: false, icon: "Code" },
    tags: [{ id: "t-agent", name: "Agent" }, { id: "t-open", name: "开源" }],
  });
  assert.equal(categoryUpdateInput(site, "c-dev"), null);
  assert.equal(categoryUpdateInput(site, ""), null);
  assert.deepEqual(categoryUpdateInput(site, "c-read"), { expectedVersion: 7, categoryId: "c-read" });

  assert.equal(tagsUpdateInput(site, ["t-open", "t-agent"]), null, "顺序不同但集合相同");
  assert.deepEqual(tagsUpdateInput(site, ["t-agent", "t-agent", "t-new"]), {
    expectedVersion: 7,
    tagIds: ["t-agent", "t-new"],
  });
  assert.deepEqual(tagsUpdateInput(site, []), { expectedVersion: 7, tagIds: [] }, "可以清空全部标签");
});

test("category options put the default category first, then sort by name", () => {
  const sorted = sortCategoryOptions([
    { id: "b", name: "阅读", isDefault: false, icon: "", siteCount: 1 },
    { id: "a", name: "未分类", isDefault: true, icon: "", siteCount: 3 },
    { id: "c", name: "开发", isDefault: false, icon: "", siteCount: 2 },
  ]);
  assert.equal(sorted[0]?.id, "a");
  assert.deepEqual(sorted.slice(1).map((category) => category.name).sort(), ["开发", "阅读"]);
});

test("contract: an inline tag edit PATCHes only expected_version and tag_ids", async (context) => {
  const requests = captureFetch(context);
  const site = siteFixture({ id: "site/1", version: 7, tags: [{ id: "t-agent", name: "Agent" }] });
  const input = tagsUpdateInput(site, ["t-agent", "t-open"]);
  assert.ok(input);
  const saved = await updateLibrarySite(site.id, input);

  assert.equal(requests.length, 1);
  assert.equal(requests[0]?.url, "/api/backend/library/sites/site%2F1");
  assert.equal(requests[0]?.init?.method, "PATCH");
  assert.deepEqual(JSON.parse(String(requests[0]?.init?.body)), {
    expected_version: 7,
    tag_ids: ["t-agent", "t-open"],
  });
  assert.equal(saved.version, 7);
});

test("contract: an inline category edit PATCHes only expected_version and category_id", async (context) => {
  const requests = captureFetch(context);
  const site = siteFixture({ version: 3 });
  const input = categoryUpdateInput(site, "c-read");
  assert.ok(input);
  await updateLibrarySite(site.id, input);
  assert.deepEqual(JSON.parse(String(requests[0]?.init?.body)), {
    expected_version: 3,
    category_id: "c-read",
  });
});

// ── 收藏库管理模式的契约补充 ────────────────────────────────────────────

test("contract: the Space filter reaches both the list and the select-all snapshot", async (context) => {
  const requests = captureFetch(context, {
    items: [],
    next_cursor: null,
    aggregate: { matched_count: 0, pinned_count: 0 },
  });
  await listLibrarySites({ spaceId: " space-1 ", kind: "repo", state: "unopened", sort: "opened" });
  const listed = new URL(requests[0]?.url ?? "", "http://x").searchParams;
  assert.equal(listed.get("space_id"), "space-1");
  assert.equal(listed.get("kind"), "repo");
  assert.equal(listed.get("state"), "unopened");
  assert.equal(listed.get("sort"), "opened");

  const selectionRequests = captureFetch(context, { items: [] });
  await listLibrarySiteSelection({ spaceId: "space-1", state: "starred", sort: "name", limit: 5 });
  const selection = new URL(selectionRequests[0]?.url ?? "", "http://x");
  assert.equal(selection.pathname, "/api/backend/library/sites/selection");
  assert.equal(selection.searchParams.get("space_id"), "space-1");
  assert.equal(selection.searchParams.get("state"), "starred");
  assert.equal(selection.searchParams.has("sort"), false);
  assert.equal(selection.searchParams.has("limit"), false);
});

test("contract: snooze bookkeeping is parsed, and absent fields read as never snoozed", () => {
  const legacy = normalizeLibrarySite(apiSite);
  assert.equal(legacy.resurfaceCount, 0);
  assert.equal(legacy.resurfaceDueAt, null);

  const snoozed = normalizeLibrarySite({
    ...apiSite,
    resurface_count: 2,
    resurface_due_at: "2026-10-29T12:00:00",
  });
  assert.equal(snoozed.resurfaceCount, 2);
  assert.equal(snoozed.resurfaceDueAt, "2026-10-29T12:00:00");

  assert.throws(() => normalizeLibrarySite({ ...apiSite, resurface_count: -1 }));
  assert.throws(() => normalizeLibrarySite({ ...apiSite, resurface_due_at: "soon" }));
});
