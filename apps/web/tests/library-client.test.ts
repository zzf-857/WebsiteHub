import assert from "node:assert/strict";
import test from "node:test";

import {
  buildLibrarySiteSearchParams,
  createLibraryCategory,
  createLibrarySite,
  listLibrarySites,
  MAX_LIBRARY_PAGE_SIZE,
  updateLibraryTag,
  updateLibrarySite,
} from "../lib/library-client.ts";

const site = {
  id: "site-1",
  name: "MDN",
  original_url: "https://developer.mozilla.org/",
  identity_url: "https://developer.mozilla.org",
  description: null,
  favicon_url: null,
  category: { id: "category-1", name: "开发", is_default: true },
  tags: [],
  pinned: false,
  source: "manual",
  analysis_status: "not_analyzed",
  version: 1,
  created_at: "2026-07-25T10:00:00Z",
  updated_at: "2026-07-25T10:00:00Z",
};

test("builds bounded snake-case site list queries", () => {
  const params = buildLibrarySiteSearchParams({
    q: "  docs  ",
    categoryId: "category-1",
    tagId: "tag-1",
    pinned: false,
    sort: "name",
    direction: "asc",
    cursor: "cursor value",
    limit: 999,
  });
  assert.equal(params.get("q"), "docs");
  assert.equal(params.get("category_id"), "category-1");
  assert.equal(params.get("tag_id"), "tag-1");
  assert.equal(params.get("pinned"), "false");
  assert.equal(params.get("sort"), "name");
  assert.equal(params.get("direction"), "asc");
  assert.equal(params.get("limit"), String(MAX_LIBRARY_PAGE_SIZE));
});

test("list requests keep cookies and normalize the response", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let capturedInput: string | URL | Request | undefined;
  let capturedInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    capturedInput = input;
    capturedInit = init;
    return new Response(JSON.stringify({
      items: [site],
      next_cursor: null,
      aggregate: { matched_count: 1, pinned_count: 0 },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };

  const result = await listLibrarySites({ q: "MDN" });
  assert.match(String(capturedInput), /^\/api\/backend\/library\/sites\?/);
  assert.equal(capturedInit?.credentials, "include");
  assert.equal(capturedInit?.cache, "no-store");
  assert.equal(result.items[0]?.name, "MDN");
});

test("create and update serialize the documented request bodies", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ input: String(input), init });
    return new Response(JSON.stringify(site), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  await createLibrarySite({
    name: "MDN",
    url: "https://developer.mozilla.org",
    description: null,
    categoryId: "category-1",
    tagIds: ["tag-1"],
    pinned: true,
  });
  await updateLibrarySite("site/one", { expectedVersion: 3, pinned: false });
  await updateLibrarySite("site/one", { expectedVersion: 4, categoryId: null });

  assert.equal(requests[0]?.input, "/api/backend/library/sites");
  assert.deepEqual(JSON.parse(String(requests[0]?.init?.body)), {
    name: "MDN",
    url: "https://developer.mozilla.org",
    category_id: "category-1",
    tag_ids: ["tag-1"],
    pinned: true,
  });
  assert.equal(requests[1]?.input, "/api/backend/library/sites/site%2Fone");
  assert.deepEqual(JSON.parse(String(requests[1]?.init?.body)), {
    expected_version: 3,
    pinned: false,
  });
  assert.deepEqual(JSON.parse(String(requests[2]?.init?.body)), {
    expected_version: 4,
    category_id: null,
  });
});

test("category and tag writes reject names beyond backend limits before fetching", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return new Response(JSON.stringify(site), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  await assert.rejects(createLibraryCategory("c".repeat(81)), /80/);
  await assert.rejects(updateLibraryTag("tag-1", "t".repeat(41)), /40/);
  assert.equal(fetchCount, 0);
});
