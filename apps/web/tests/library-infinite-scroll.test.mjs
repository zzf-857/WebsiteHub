import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  canStartLibraryPageLoad,
  libraryAutoLoadMode,
} from "../lib/library-pagination.ts";

const AUTO_LOAD = new URL(
  "../components/library/library-auto-load.tsx",
  import.meta.url,
);
const WORKSPACE = new URL(
  "../components/library/library-workspace.tsx",
  import.meta.url,
);
const WORKSPACE_HOOK = new URL(
  "../components/library/use-library-workspace.tsx",
  import.meta.url,
);

test("library pages load silently before their sentinel reaches the viewport", async () => {
  const [autoLoad, workspace] = await Promise.all([
    readFile(AUTO_LOAD, "utf8"),
    readFile(WORKSPACE, "utf8"),
  ]);

  assert.match(autoLoad, /new IntersectionObserver\(/);
  assert.match(autoLoad, /0px 0px 640px 0px/);
  assert.match(autoLoad, /entries\.some\(\(entry\) => entry\.isIntersecting\)/);
  assert.match(autoLoad, /mode === "hidden"/);
  assert.match(autoLoad, /mode === "fallback"/);
  assert.match(autoLoad, /<button/);
  assert.match(autoLoad, /aria-live="polite"/);

  assert.equal(
    workspace.match(/<LibraryAutoLoad/g)?.length,
    2,
    "置顶和普通网站各自保留独立分页哨兵",
  );
  assert.match(workspace, /onLoadMore=\{\(\) => loadMore\("pinned"\)\}/);
  assert.match(workspace, /onLoadMore=\{\(\) => loadMore\("regular"\)\}/);
  assert.doesNotMatch(workspace, /library-load-more|onClick=\{\(\) => void loadMore/);
});

test("library pagination rejects duplicate and stale requests", async () => {
  const source = await readFile(WORKSPACE_HOOK, "utf8");

  assert.match(source, /canStartLibraryPageLoad\(loadState\)/);
  assert.match(source, /paginationFailedCursor\.current\[kind\] = cursor/);
  assert.match(source, /paginationControllers\.current\[kind\] = controller/);
  assert.match(source, /listLibrarySites\([\s\S]*?\}, controller\.signal\)/);
  assert.match(source, /controller\.signal\.aborted[\s\S]*?requestGeneration\.current !== generation/);
  assert.match(source, /activeSiteQueryScope\.current !== queryScope/);
  assert.match(source, /requestGeneration\.current \+= 1;[\s\S]*?cancelPaginationRequests\(true\)/);
  assert.match(source, /paginationControllers\.current\[kind\]\?\.abort\(\)/);

  const loadMoreSource = source.slice(
    source.indexOf("const loadMore"),
    source.indexOf("const toggleAllMatchingSites"),
  );
  assert.doesNotMatch(loadMoreSource, /setSitesError\(null\)/);
});

test("auto-load falls back only when observation is unavailable", () => {
  assert.equal(libraryAutoLoadMode(false, false), "hidden");
  assert.equal(libraryAutoLoadMode(true, null), "sentinel");
  assert.equal(libraryAutoLoadMode(true, true), "sentinel");
  assert.equal(libraryAutoLoadMode(true, false), "fallback");
});

test("failed cursors stay blocked until a full refresh clears them", () => {
  const ready = {
    nextCursor: "cursor-2",
    loading: false,
    inFlight: false,
    failedCursor: null,
  };
  assert.equal(canStartLibraryPageLoad(ready), true);
  assert.equal(canStartLibraryPageLoad({ ...ready, loading: true }), false);
  assert.equal(canStartLibraryPageLoad({ ...ready, inFlight: true }), false);
  assert.equal(canStartLibraryPageLoad({ ...ready, failedCursor: "cursor-2" }), false);
  assert.equal(canStartLibraryPageLoad({ ...ready, failedCursor: "cursor-1" }), true);
  assert.equal(canStartLibraryPageLoad({ ...ready, nextCursor: null }), false);
});
