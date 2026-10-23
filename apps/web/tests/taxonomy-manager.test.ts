import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { CATEGORY_ICON_OPTIONS, categoryIconLabel, isCategoryIconName } from "../lib/category-icons.ts";
import {
  mergeLibraryTag,
  previewLibraryTagMerge,
  updateLibraryCategory,
  createLibraryCategory,
} from "../lib/library-client.ts";
import { LibraryContractError, type LibraryTag, type LibraryTagMergePreview } from "../lib/library-contract.ts";
import { tagMergeSummary, tagMergeTargetOptions } from "../lib/tag-merge.ts";

// Q52：分类图标选择器与标签合并。

const read = (path: string) => readFile(new URL(`../${path}`, import.meta.url), "utf8");

function tag(id: string, name: string, siteCount = 0): LibraryTag {
  return { id, name, siteCount } as LibraryTag;
}

const rawTag = (id: string, name: string, count: number) => ({
  id,
  name,
  site_count: count,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
});

const rawCategory = {
  id: "cat-1",
  name: "阅读",
  is_default: false,
  icon: "Coffee",
  site_count: 2,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
};

type Captured = { url: string; init: RequestInit | undefined };

function mockFetch(context: test.TestContext, body: unknown, status = 200): Captured[] {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const calls: Captured[] = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ url: String(input), init });
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  };
  return calls;
}

// ── 图标白名单 ───────────────────────────────────────────────────────────

test("the icon whitelist is unique and matches what DynamicIcon can render", async () => {
  const names = CATEGORY_ICON_OPTIONS.map((option) => option.name);
  assert.equal(new Set(names).size, names.length);
  assert.ok(CATEGORY_ICON_OPTIONS.every((option) => option.label.trim().length > 0));
  const renderer = await read("components/ui/dynamic-icon.tsx");
  const body = renderer.split("ICON_MAP", 2)[1].split("};", 1)[0];
  const rendered = new Set(body.match(/\b[A-Z][A-Za-z0-9]+\b/gu)?.filter((name) => !["Record", "React", "FC", "LucideProps"].includes(name)));
  assert.deepEqual([...rendered].sort(), [...names].sort());
  for (const forbidden of ["Loader2", "AlertCircle", "StopCircle"]) assert.equal(isCategoryIconName(forbidden), false);
});

test("icon labels read the fallback for unknown stored icons", () => {
  assert.equal(categoryIconLabel("Coffee"), "生活");
  assert.equal(categoryIconLabel("SomethingOld"), "文件夹");
  assert.equal(isCategoryIconName("Star"), true);
  assert.equal(isCategoryIconName("star"), false);
});

test("category writes send the icon only when one was chosen", async (context) => {
  const calls = mockFetch(context, rawCategory);
  await updateLibraryCategory("cat-1", "阅读", "Coffee");
  await updateLibraryCategory("cat-1", "阅读");
  await updateLibraryCategory("cat-1", "阅读", "");
  await createLibraryCategory("新分类", " Star ");
  assert.deepEqual(calls.map((call) => JSON.parse(String(call.init?.body))), [
    { name: "阅读", icon: "Coffee" },
    { name: "阅读" },
    { name: "阅读", icon: "" },
    { name: "新分类", icon: "Star" },
  ]);
  assert.equal(calls[0].init?.method, "PATCH");
  assert.match(calls[0].url, /\/api\/backend\/library\/categories\/cat-1$/u);
});

test("an icon outside the whitelist is rejected before fetching", async (context) => {
  const calls = mockFetch(context, rawCategory);
  await assert.rejects(updateLibraryCategory("cat-1", "阅读", "Loader2"), /不支持的分类图标/u);
  await assert.rejects(createLibraryCategory("阅读", "<svg>"), /不支持的分类图标/u);
  assert.equal(calls.length, 0);
});

// ── 标签合并 ─────────────────────────────────────────────────────────────

test("merge preview and merge hit the account-scoped endpoints with strict bodies", async (context) => {
  const preview = {
    source: rawTag("t-a", "LLM", 3),
    target: rawTag("t-b", "大模型", 5),
    affected_site_count: 3,
    already_tagged_count: 1,
  };
  const calls = mockFetch(context, preview);
  const parsed = await previewLibraryTagMerge("t-a", "t-b");
  assert.equal(parsed.affectedSiteCount, 3);
  assert.equal(parsed.alreadyTaggedCount, 1);
  assert.equal(parsed.target.name, "大模型");
  assert.match(calls[0].url, /\/library\/tags\/t-a\/merge-preview\?target_id=t-b$/u);
  assert.equal(calls[0].init?.method, undefined);

  const merged = { message: "已合并", target: rawTag("t-b", "大模型", 7), affected_site_count: 3, already_tagged_count: 1 };
  const mergeCalls = mockFetch(context, merged);
  const result = await mergeLibraryTag("t-a", "t-b");
  assert.equal(result.target.siteCount, 7);
  assert.equal(mergeCalls[0].init?.method, "POST");
  assert.match(mergeCalls[0].url, /\/library\/tags\/t-a\/merge$/u);
  assert.deepEqual(JSON.parse(String(mergeCalls[0].init?.body)), { target_id: "t-b" });
});

test("merging a tag into itself is refused locally, and malformed responses fail the contract", async (context) => {
  const calls = mockFetch(context, { source: rawTag("t-a", "a", 1) });
  await assert.rejects(mergeLibraryTag("t-a", " t-a "), /自己/u);
  await assert.rejects(previewLibraryTagMerge("t-a", "t-a"), /自己/u);
  assert.equal(calls.length, 0);
  await assert.rejects(previewLibraryTagMerge("t-a", "t-b"), LibraryContractError);
});

test("merge targets exclude the source and keep the list order", () => {
  const options = tagMergeTargetOptions([tag("1", "Agent", 4), tag("2", "LLM", 1), tag("3", "向量", 2)], "2");
  assert.deepEqual(options.map((option) => option.value), ["1", "3"]);
  assert.equal(options[0].label, "Agent（4）");
  assert.deepEqual(options[0].keywords, ["Agent"]);
  assert.deepEqual(tagMergeTargetOptions([tag("1", "only")], "1"), []);
});

test("the merge summary states the affected count before the second confirmation", () => {
  const preview = (affected: number, overlap: number): LibraryTagMergePreview => ({
    source: tag("a", "LLM", affected),
    target: tag("b", "大模型", 5),
    affectedSiteCount: affected,
    alreadyTaggedCount: overlap,
  });
  assert.equal(
    tagMergeSummary(preview(3, 1)),
    "3 个收藏会改带「大模型」，其中 1 个原本就带「大模型」（新增关联 2 个）；合并后删除「LLM」，不能撤销。",
  );
  assert.equal(
    tagMergeSummary(preview(2, 0)),
    "2 个收藏会改带「大模型」（新增关联 2 个）；合并后删除「LLM」，不能撤销。",
  );
  assert.equal(tagMergeSummary(preview(0, 0)), "「LLM」没有关联收藏，合并后直接删除，「大模型」不变。");
});

// ── 接线 ─────────────────────────────────────────────────────────────────

test("the taxonomy manager wires the icon picker and the two-step merge", async () => {
  const manager = await read("components/library/taxonomy-manager.tsx");
  assert.match(manager, /CATEGORY_ICON_OPTIONS\.map/u);
  assert.match(manager, /aria-pressed=\{category\.icon === option\.name\}/u);
  assert.match(manager, /updateLibraryCategory\(category\.id, category\.name, icon\)/u);
  assert.match(manager, /chooseIcon\(category, ""\)/u, "可以恢复自动推断");
  assert.match(manager, /event\.key !== "Escape"/u);
  assert.match(manager, /disabled=\{busy \|\| isDefault\}/u, "默认分类不能改图标");
  assert.match(manager, /previewLibraryTagMerge\(mergeSourceId, mergeTargetId, controller\.signal\)/u);
  assert.match(manager, /disabled=\{busy \|\| !mergeReady\}/u, "预览就绪前不能确认");
  assert.match(manager, /mergeLibraryTag\(target\.source\.id, target\.targetId\)/u);
  assert.match(manager, /tagMergeSummary\(merge\.preview\)/u);
});
