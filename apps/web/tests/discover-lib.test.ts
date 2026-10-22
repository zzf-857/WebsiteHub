import assert from "node:assert/strict";
import test from "node:test";

import {
  heroActive,
  heroDismissInput,
  heroPosition,
  heroReducer,
  heroToastMessage,
  heroUndoInput,
  heroUpNext,
  initialHeroState,
  type HeroEntry,
  type HeroState,
} from "../lib/discover-hero.ts";
import {
  appendMasonryItems,
  createMasonryLayout,
  layoutMasonry,
  masonryColumnCount,
} from "../lib/masonry.ts";
import { duplicateSiteHref, isDuplicateSiteError, parseQuickAddUrl } from "../lib/quick-add.ts";
import { formatRelativeTime, formatSavedDays, parseServerTime } from "../lib/relative-time.ts";
import { isUnopened, withSiteOpened, withSiteState } from "../lib/site-usage.ts";
import { siteFixture } from "./discover-fixtures.ts";

/* ── 瀑布流 ─────────────────────────────────────────────── */

test("masonry column count follows the container width breakpoints", () => {
  assert.equal(masonryColumnCount(1360), 4);
  assert.equal(masonryColumnCount(1280), 4);
  assert.equal(masonryColumnCount(1279), 3);
  assert.equal(masonryColumnCount(960), 3);
  assert.equal(masonryColumnCount(959), 2);
  assert.equal(masonryColumnCount(600), 2);
  assert.equal(masonryColumnCount(599), 1);
  assert.equal(masonryColumnCount(358), 1);
  assert.equal(masonryColumnCount(Number.NaN), 1);
});

test("masonry places each item into the currently shortest column", () => {
  const layout = layoutMasonry(
    [
      { id: "a", height: 300 },
      { id: "b", height: 100 },
      { id: "c", height: 200 },
      { id: "d", height: 50 },
      { id: "e", height: 80 },
    ],
    3,
    10,
  );
  // a→0，b→1，c→2；此时 [300,100,200]，d→1（110+50=160）；e→1 仍最短？[300,160,200] → e→1
  assert.deepEqual(layout.columns, [["a"], ["b", "d", "e"], ["c"]]);
  assert.deepEqual(layout.heights, [300, 100 + 10 + 50 + 10 + 80, 200]);
});

test("ties go to the leftmost column", () => {
  const layout = layoutMasonry([{ id: "a", height: 10 }, { id: "b", height: 10 }, { id: "c", height: 10 }], 2);
  assert.deepEqual(layout.columns, [["a", "c"], ["b"]]);
});

test("appending a new page never moves already placed items", () => {
  const firstPage = [
    { id: "a", height: 260 },
    { id: "b", height: 180 },
    { id: "c", height: 220 },
    { id: "d", height: 300 },
  ];
  const secondPage = [
    { id: "e", height: 120 },
    { id: "f", height: 400 },
    { id: "g", height: 90 },
  ];
  const before = layoutMasonry(firstPage, 3, 28);
  const after = appendMasonryItems(before, secondPage, 28);
  before.columns.forEach((column, index) => {
    assert.deepEqual(after.columns[index].slice(0, column.length), column);
  });
  // 一次排完与分页追加的结果完全一致
  assert.deepEqual(after, layoutMasonry([...firstPage, ...secondPage], 3, 28));
  // 入参不被修改
  assert.equal(before.columns.flat().length, 4);
});

test("duplicate ids from overlapping pages are placed only once", () => {
  const layout = appendMasonryItems(layoutMasonry([{ id: "a", height: 10 }], 2), [
    { id: "a", height: 10 },
    { id: "b", height: 10 },
  ]);
  assert.deepEqual(layout.columns.flat().sort(), ["a", "b"]);
});

test("changing the column count re-lays out every item", () => {
  const items = Array.from({ length: 8 }, (_, index) => ({ id: `s${index}`, height: 100 + index * 10 }));
  for (const count of [1, 2, 3, 4]) {
    const layout = layoutMasonry(items, count);
    assert.equal(layout.columns.length, count);
    assert.deepEqual(layout.columns.flat().sort(), items.map((item) => item.id).sort());
  }
  assert.equal(createMasonryLayout(0).columns.length, 1);
});

/* ── 主卡队列 ───────────────────────────────────────────── */

function entry(id: string, savedDays = 10): HeroEntry {
  return { site: siteFixture({ id, name: `收藏 ${id}` }), savedDays, reason: `${savedDays} 天前收藏，一次都没打开过` };
}

function ids(state: HeroState): string[] {
  return state.entries.map((item) => item.site.id);
}

test("the hero starts on the first candidate and lists the rest as up next", () => {
  const state = initialHeroState([entry("a"), entry("b"), entry("c"), entry("d")]);
  assert.equal(heroActive(state)?.site.id, "a");
  assert.deepEqual(heroPosition(state), { index: 0, total: 4 });
  assert.deepEqual(heroUpNext(state).map((item) => item.site.id), ["b", "c", "d"]);
  assert.equal(heroActive(initialHeroState()), null);
});

test("selecting a pager bar or thumb promotes it without reordering", () => {
  let state = initialHeroState([entry("a"), entry("b"), entry("c"), entry("d")]);
  state = heroReducer(state, { type: "select", id: "c" });
  assert.equal(heroActive(state)?.site.id, "c");
  assert.deepEqual(ids(state), ["a", "b", "c", "d"]);
  assert.deepEqual(heroPosition(state), { index: 2, total: 4 });
  // “接下来”绕回队首
  assert.deepEqual(heroUpNext(state).map((item) => item.site.id), ["d", "a", "b"]);
  assert.equal(heroReducer(state, { type: "select", id: "missing" }), state);
});

test("dismissing the active item lets the next one take its place", () => {
  let state = initialHeroState([entry("a"), entry("b"), entry("c")]);
  state = heroReducer(state, { type: "dismiss", id: "a", dismissal: "seen" });
  assert.deepEqual(ids(state), ["b", "c"]);
  assert.equal(heroActive(state)?.site.id, "b");

  state = heroReducer(state, { type: "select", id: "c" });
  state = heroReducer(state, { type: "dismiss", id: "c", dismissal: "snooze" });
  // 处理最后一项时回到队首
  assert.equal(heroActive(state)?.site.id, "b");
});

test("dismissing a non-active item keeps the current hero", () => {
  let state = initialHeroState([entry("a"), entry("b"), entry("c")]);
  state = heroReducer(state, { type: "dismiss", id: "c", dismissal: "mute" });
  assert.equal(heroActive(state)?.site.id, "a");
  assert.deepEqual(ids(state), ["a", "b"]);
});

test("undo restores the item to its original slot and makes it the hero again", () => {
  let state = initialHeroState([entry("a"), entry("b"), entry("c"), entry("d")]);
  state = heroReducer(state, { type: "select", id: "b" });
  state = heroReducer(state, { type: "dismiss", id: "b", dismissal: "seen" });
  assert.equal(heroActive(state)?.site.id, "c");
  state = heroReducer(state, { type: "restore", id: "b" });
  assert.deepEqual(ids(state), ["a", "b", "c", "d"]);
  assert.equal(heroActive(state)?.site.id, "b");
  // 同一条不能被恢复两次
  assert.equal(heroReducer(state, { type: "restore", id: "b" }), state);
});

test("an older dismissal can still be rolled back after a newer one", () => {
  let state = initialHeroState([entry("a"), entry("b"), entry("c")]);
  state = heroReducer(state, { type: "dismiss", id: "a", dismissal: "seen" });
  state = heroReducer(state, { type: "dismiss", id: "b", dismissal: "mute" });
  state = heroReducer(state, { type: "restore", id: "a" });
  assert.deepEqual(ids(state), ["a", "c"]);
  state = heroReducer(state, { type: "restore", id: "b" });
  assert.deepEqual(ids(state), ["a", "b", "c"]);
});

test("exhausting the queue leaves no hero, and undo brings it back", () => {
  let state = initialHeroState([entry("a"), entry("b")]);
  state = heroReducer(state, { type: "dismiss", id: "a", dismissal: "open" });
  state = heroReducer(state, { type: "dismiss", id: "b", dismissal: "seen" });
  assert.equal(heroActive(state), null);
  assert.deepEqual(heroPosition(state), { index: 0, total: 0 });
  assert.deepEqual(heroUpNext(state), []);
  state = heroReducer(state, { type: "restore", id: "b" });
  assert.equal(heroActive(state)?.site.id, "b");
});

test("loading a new batch resets the queue and the undo history", () => {
  let state = initialHeroState([entry("a")]);
  state = heroReducer(state, { type: "dismiss", id: "a", dismissal: "seen" });
  state = heroReducer(state, { type: "load", entries: [entry("x"), entry("y")] });
  assert.deepEqual(ids(state), ["x", "y"]);
  assert.equal(state.removed.length, 0);
  assert.equal(heroReducer(state, { type: "restore", id: "a" }), state);
});

test("site updates replace the entry data in the queue and the undo history", () => {
  let state = initialHeroState([entry("a"), entry("b")]);
  state = heroReducer(state, { type: "dismiss", id: "b", dismissal: "seen" });
  state = heroReducer(state, { type: "update", site: siteFixture({ id: "a", starred: true, starredAt: "2026-10-20T00:00:00Z" }) });
  state = heroReducer(state, { type: "update", site: siteFixture({ id: "b", name: "改过的名字" }) });
  assert.equal(heroActive(state)?.site.starred, true);
  state = heroReducer(state, { type: "restore", id: "b" });
  assert.equal(heroActive(state)?.site.name, "改过的名字");
});

test("dismissals map to state updates, inverse updates and toast copy", () => {
  assert.deepEqual(heroDismissInput("seen"), { seen: true });
  assert.deepEqual(heroDismissInput("snooze"), { snoozeDays: 7 });
  assert.deepEqual(heroDismissInput("mute"), { resurfaceMuted: true });
  assert.equal(heroDismissInput("open"), null);
  assert.deepEqual(heroUndoInput("seen"), { seen: false });
  assert.deepEqual(heroUndoInput("snooze"), { snoozeDays: 0 });
  assert.deepEqual(heroUndoInput("mute"), { resurfaceMuted: false });
  assert.equal(heroUndoInput("open"), null);
  assert.equal(heroToastMessage("seen"), "已标记为已看");
  assert.equal(heroToastMessage("snooze"), "下周再提醒你");
  assert.equal(heroToastMessage("mute"), "不再推荐");
  assert.equal(heroToastMessage("open"), null);
});

/* ── 相对时间 ───────────────────────────────────────────── */

test("server timestamps without an offset are read as UTC", () => {
  assert.equal(parseServerTime("2026-10-20T08:00:00"), Date.parse("2026-10-20T08:00:00Z"));
  assert.equal(parseServerTime("2026-10-20T08:00:00+08:00"), Date.parse("2026-10-20T00:00:00Z"));
  assert.equal(parseServerTime("2026-10-20T08:00:00.123456Z"), Date.parse("2026-10-20T08:00:00.123Z"));
  assert.ok(Number.isNaN(parseServerTime("")));
  assert.ok(Number.isNaN(parseServerTime("yesterday")));
});

test("relative saved time uses minutes, hours, days, months and years", () => {
  const now = Date.parse("2026-10-22T12:00:00Z");
  assert.equal(formatRelativeTime("2026-10-22T11:59:30", now), "刚刚");
  assert.equal(formatRelativeTime("2026-10-22T12:05:00", now), "刚刚", "时钟偏差不显示负数");
  assert.equal(formatRelativeTime("2026-10-22T11:59:00", now), "1 分钟前");
  assert.equal(formatRelativeTime("2026-10-22T11:01:00", now), "59 分钟前");
  assert.equal(formatRelativeTime("2026-10-22T11:00:00", now), "1 小时前");
  assert.equal(formatRelativeTime("2026-10-21T12:00:01", now), "23 小时前");
  assert.equal(formatRelativeTime("2026-10-21T12:00:00", now), "1 天前");
  assert.equal(formatRelativeTime("2026-09-23T12:00:00", now), "29 天前");
  assert.equal(formatRelativeTime("2026-09-22T12:00:00", now), "1 个月前");
  assert.equal(formatRelativeTime("2026-01-01T00:00:00", now), "9 个月前");
  assert.equal(formatRelativeTime("2024-10-01T00:00:00", now), "2 年前");
  assert.equal(formatRelativeTime("broken", now), "");
});

test("hero saved-days copy always counts days", () => {
  assert.equal(formatSavedDays(0), "今天收藏");
  assert.equal(formatSavedDays(3), "3 天前收藏");
  assert.equal(formatSavedDays(43), "43 天前收藏");
});

/* ── 快速收藏 ───────────────────────────────────────────── */

test("quick add accepts http(s) links and bare domains, naming them by host", () => {
  assert.deepEqual(parseQuickAddUrl(" https://www.example.com/a?b=1 "), {
    ok: true,
    url: "https://www.example.com/a?b=1",
    name: "example.com",
  });
  assert.deepEqual(parseQuickAddUrl("github.com/ollama/ollama"), {
    ok: true,
    url: "https://github.com/ollama/ollama",
    name: "github.com",
  });
  assert.equal(parseQuickAddUrl("").ok, false);
  assert.equal(parseQuickAddUrl("hello world").ok, false);
  assert.equal(parseQuickAddUrl("javascript:alert(1)").ok, false);
  assert.equal(parseQuickAddUrl("ftp://example.com/file").ok, false);
  assert.equal(parseQuickAddUrl("https://user:pw@example.com").ok, false);
});

test("duplicate detection and the fallback link for an existing save", () => {
  assert.equal(isDuplicateSiteError({ status: 409, code: "duplicate_url" }), true);
  assert.equal(isDuplicateSiteError({ status: 409 }), true);
  assert.equal(isDuplicateSiteError({ status: 409, code: "version_conflict" }), false);
  assert.equal(isDuplicateSiteError({ status: 422 }), false);
  assert.equal(isDuplicateSiteError(null), false);
  assert.equal(duplicateSiteHref("https://a.com/?x=1&y=2"), "/library?q=https%3A%2F%2Fa.com%2F%3Fx%3D1%26y%3D2");
});

/* ── 乐观更新 ───────────────────────────────────────────── */

test("optimistic usage updates mirror the backend semantics", () => {
  const now = "2026-10-22T12:00:00Z";
  const site = siteFixture();
  assert.equal(isUnopened(site), true);

  const opened = withSiteOpened(site, now);
  assert.equal(opened.openCount, 1);
  assert.equal(opened.lastOpenedAt, now);
  assert.equal(opened.seenAt, now);
  assert.equal(isUnopened(opened), false);
  assert.equal(site.openCount, 0, "不修改入参");

  const starred = withSiteState(site, { starred: true }, now);
  assert.equal(starred.starred, true);
  assert.equal(starred.starredAt, now);
  assert.equal(withSiteState(starred, { starred: false }, now).starredAt, null);

  const seen = withSiteState(site, { seen: true }, now);
  assert.equal(isUnopened(seen), false);
  assert.equal(withSiteState(seen, { seen: false }, now).seenAt, null);
  assert.equal(withSiteState(site, { resurfaceMuted: true }, now).resurfaceMuted, true);
});
