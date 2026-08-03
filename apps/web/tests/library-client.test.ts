import assert from "node:assert/strict";
import test from "node:test";

import {
  backfillLibrarySiteMetadata,
  buildLibrarySiteSearchParams,
  createLibraryCategory,
  createLibrarySite,
  confirmAgentReclassify,
  deleteLibrarySites,
  deleteLibrarySite,
  applySiteSimilarityScan,
  getActiveSiteSimilarityScan,
  getMetadataBackfillPlan,
  LibraryApiError,
  listLibrarySites,
  listSiteSimilarityGroups,
  MAX_LIBRARY_PAGE_SIZE,
  startMetadataBackfill,
  startSiteSimilarityScan,
  saveSiteSimilarityDecision,
  selectRecommendedSiteSimilarityDecisions,
  updateLibraryTag,
  updateLibrarySite,
} from "../lib/library-client.ts";

const site = {
  id: "site-1",
  name: "MDN",
  original_url: "https://developer.mozilla.org/",
  identity_url: "https://developer.mozilla.org",
  summary: null,
  description: null,
  favicon_url: null,
  preview_url: null,
  category: { id: "category-1", name: "开发", is_default: true, icon: "Code" },
  tags: [],
  pinned: false,
  source: "manual",
  analysis_status: "not_analyzed",
  analysis_phase: null,
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

test("site writes serialize bodies and delete optimistic concurrency", async (context) => {
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
    summary: "提供权威且系统的 Web 开发标准参考资料",
    description: null,
    categoryId: "category-1",
    tagIds: ["tag-1"],
    pinned: true,
  });
  await updateLibrarySite("site/one", { expectedVersion: 3, pinned: false });
  await updateLibrarySite("site/one", { expectedVersion: 4, categoryId: null });
  await deleteLibrarySite("site/one", 5);

  assert.equal(requests[0]?.input, "/api/backend/library/sites");
  assert.deepEqual(JSON.parse(String(requests[0]?.init?.body)), {
    name: "MDN",
    url: "https://developer.mozilla.org",
    summary: "提供权威且系统的 Web 开发标准参考资料",
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
  assert.equal(requests[3]?.input, "/api/backend/library/sites/site%2Fone?expected_version=5");
  assert.equal(requests[3]?.init?.method, "DELETE");
});

test("metadata backfill is an explicit bounded request", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let capturedInput: string | URL | Request | undefined;
  let capturedInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    capturedInput = input;
    capturedInit = init;
    return new Response(JSON.stringify({
      queued_count: 100,
      active_count: 100,
      remaining_count: 1927,
    }), { status: 202, headers: { "Content-Type": "application/json" } });
  };

  assert.deepEqual(await backfillLibrarySiteMetadata(100), {
    queuedCount: 100,
    activeCount: 100,
    remainingCount: 1927,
  });
  assert.equal(capturedInput, "/api/backend/library/sites/analyze-missing?limit=100");
  assert.equal(capturedInit?.method, "POST");
  await assert.rejects(backfillLibrarySiteMetadata(5_001), /1 到 5000/);
});

test("durable metadata backfill previews and starts the exact selected plan", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  const responses = [
    {
      mode: "full",
      requested_limit: 50,
      eligible_count: 87,
      selected_count: 50,
      llm_count: 42,
      max_limit: 100,
    },
    {
      id: "run-1",
      mode: "full",
      status: "queued",
      stopped_early: false,
      stop_reason: null,
      provider_retry_at: null,
      total_count: 50,
      queued_count: 50,
      running_count: 0,
      completed_count: 0,
      complete_count: 0,
      limited_count: 0,
      failed_count: 0,
      skipped_count: 0,
      reused: false,
    },
  ];
  globalThis.fetch = async (input, init) => {
    requests.push({ input: String(input), init });
    return new Response(JSON.stringify(responses.shift()), {
      status: init?.method === "POST" ? 202 : 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  const plan = await getMetadataBackfillPlan({ mode: "full", limit: 50 });
  const progress = await startMetadataBackfill({ mode: "full", limit: 50 });

  assert.equal(plan.selectedCount, 50);
  assert.equal(plan.llmCount, 42);
  assert.equal(progress.mode, "full");
  assert.equal(
    requests[0]?.input,
    "/api/backend/library/metadata-backfills/plan?mode=full&limit=50",
  );
  assert.equal(requests[0]?.init?.credentials, "include");
  assert.equal(requests[1]?.input, "/api/backend/library/metadata-backfills");
  assert.equal(requests[1]?.init?.method, "POST");
  assert.deepEqual(JSON.parse(String(requests[1]?.init?.body)), {
    mode: "full",
    limit: 50,
  });
});

test("metadata backfill client rejects unknown modes and stop reasons", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return new Response(JSON.stringify({
      id: "run-1",
      mode: "full",
      status: "failed",
      stopped_early: true,
      stop_reason: "provider_timeout",
      provider_retry_at: null,
      total_count: 1,
      queued_count: 0,
      running_count: 0,
      completed_count: 1,
      complete_count: 0,
      limited_count: 0,
      failed_count: 1,
      skipped_count: 0,
      reused: false,
    }), { status: 202, headers: { "Content-Type": "application/json" } });
  };

  await assert.rejects(
    getMetadataBackfillPlan({ mode: "legacy" as "metadata", limit: 10 }),
    /mode 不是受支持的值/,
  );
  assert.equal(fetchCount, 0);
  await assert.rejects(
    startMetadataBackfill({ mode: "full", limit: 1 }),
    /stop_reason 不是受支持的值/,
  );
  assert.equal(fetchCount, 1);
});

test("bulk delete sends one versioned request and normalizes deleted ids", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let capturedInput: string | URL | Request | undefined;
  let capturedInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    capturedInput = input;
    capturedInit = init;
    return new Response(JSON.stringify({
      message: "已删除 2 个网站",
      deleted_site_ids: ["site-1", "site-2"],
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };

  assert.deepEqual(await deleteLibrarySites([
    { siteId: "site-1", expectedVersion: 2 },
    { siteId: "site-2", expectedVersion: 7 },
  ]), { deletedSiteIds: ["site-1", "site-2"] });
  assert.equal(capturedInput, "/api/backend/library/sites/bulk-delete");
  assert.equal(capturedInit?.method, "POST");
  assert.deepEqual(JSON.parse(String(capturedInit?.body)), {
    items: [
      { site_id: "site-1", expected_version: 2 },
      { site_id: "site-2", expected_version: 7 },
    ],
  });
});

test("bulk delete rejects duplicate or empty selections before fetching", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return new Response(null, { status: 204 });
  };

  await assert.rejects(deleteLibrarySites([]), /至少需要一个/);
  await assert.rejects(deleteLibrarySites([
    { siteId: "site-1", expectedVersion: 1 },
    { siteId: "site-1", expectedVersion: 1 },
  ]), /重复网站/);
  assert.equal(fetchCount, 0);
});

test("site similarity client resumes, jumps pages, selects recommendations and applies", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  const scan = {
    id: "scan/1",
    status: "ready",
    ruleset_version: "library-site-similarity.v1",
    source_site_count: 30,
    group_count: 13,
    duplicate_group_count: 13,
    same_site_group_count: 0,
    candidate_site_count: 26,
    selected_group_count: 0,
    selected_delete_count: 0,
    version: 1,
    decision_version: 1,
    created_at: "2026-07-31T08:00:00Z",
    applied_at: null,
  };
  const responses: unknown[] = [
    scan,
    scan,
    {
      items: [{
        id: "group/1",
        kind: "duplicate",
        site_key: "example.com",
        display_host: "example.com",
        member_count: 3,
        recommended_site_id: "site-1",
        keep_site_ids: ["site-1", "site-2"],
        members: [
          { ...site, is_recommended: true },
          { ...site, id: "site-2", name: "Mirror", is_recommended: false },
          { ...site, id: "site-3", name: "Guide", is_recommended: false },
        ],
      }],
      next_cursor: null,
      page: 2,
      page_size: 12,
      total_count: 13,
      total_pages: 2,
      decision_version: 1,
    },
    {
      kind: "duplicate",
      matched_group_count: 13,
      updated_group_count: 13,
      decision_version: 2,
      selected_group_count: 13,
      selected_delete_count: 13,
    },
    {
      group_id: "group/1",
      keep_site_ids: ["site-1", "site-2"],
      decision_version: 3,
      selected_group_count: 13,
      selected_delete_count: 13,
    },
    {
      id: "scan/1",
      status: "applied",
      decision_version: 3,
      merged_group_count: 13,
      deleted_site_count: 13,
      kept_site_count: 13,
      deleted_site_ids: Array.from({ length: 13 }, (_, index) => `deleted-${index}`),
      kept_site_ids: Array.from({ length: 13 }, (_, index) => `kept-${index}`),
      applied_at: "2026-07-31T08:10:00Z",
    },
  ];
  globalThis.fetch = async (input, init) => {
    requests.push({ input: String(input), init });
    return new Response(JSON.stringify(responses.shift()), {
      status: init?.method === "POST" ? 201 : 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  assert.equal((await startSiteSimilarityScan()).runId, "scan/1");
  assert.equal((await getActiveSiteSimilarityScan())?.runId, "scan/1");
  const page = await listSiteSimilarityGroups("scan/1", {
    kind: "duplicate",
    page: 2,
    limit: 12,
  });
  assert.equal(page.items[0]?.recommendedSiteId, "site-1");
  assert.deepEqual(
    await selectRecommendedSiteSimilarityDecisions("scan/1", {
      kind: "duplicate",
      expectedVersion: 1,
    }),
    {
      kind: "duplicate",
      matchedGroupCount: 13,
      updatedGroupCount: 13,
      decisionVersion: 2,
      selectedGroupCount: 13,
      selectedDeleteCount: 13,
    },
  );
  const decision = await saveSiteSimilarityDecision("scan/1", "group/1", {
    keepSiteIds: [" site-1 ", "site-2"],
    expectedVersion: 2,
  });
  assert.equal(decision.decisionVersion, 3);
  assert.equal((await applySiteSimilarityScan("scan/1", 3)).deletedSiteCount, 13);

  assert.equal(requests[0]?.input, "/api/backend/library/site-similarity-scans");
  assert.equal(requests[0]?.init?.method, "POST");
  assert.equal(requests[1]?.input, "/api/backend/library/site-similarity-scans/active");
  assert.equal(
    requests[2]?.input,
    "/api/backend/library/site-similarity-scans/scan%2F1/groups?kind=duplicate&limit=12&page=2",
  );
  assert.equal(
    requests[3]?.input,
    "/api/backend/library/site-similarity-scans/scan%2F1/decisions/recommended",
  );
  assert.deepEqual(JSON.parse(String(requests[3]?.init?.body)), {
    kind: "duplicate",
    expected_version: 1,
  });
  assert.equal(
    requests[4]?.input,
    "/api/backend/library/site-similarity-scans/scan%2F1/groups/group%2F1/decision",
  );
  assert.deepEqual(JSON.parse(String(requests[4]?.init?.body)), {
    keep_site_ids: ["site-1", "site-2"],
    expected_version: 2,
  });
  assert.equal(
    requests[5]?.input,
    "/api/backend/library/site-similarity-scans/scan%2F1/apply",
  );
  assert.deepEqual(JSON.parse(String(requests[5]?.init?.body)), { expected_version: 3 });
});

test("site similarity client rejects invalid local decisions before fetching", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return new Response(null, { status: 204 });
  };

  await assert.rejects(
    saveSiteSimilarityDecision("scan-1", "group-1", {
      keepSiteIds: [" "],
      expectedVersion: 1,
    }),
    /不能为空/,
  );
  await assert.rejects(
    saveSiteSimilarityDecision("scan-1", "group-1", {
      keepSiteIds: ["site-1", "site-1"],
      expectedVersion: 1,
    }),
    /不能重复/,
  );
  await assert.rejects(listSiteSimilarityGroups("scan-1", { limit: 0 }), /正整数/);
  await assert.rejects(listSiteSimilarityGroups("scan-1", { page: 0 }), /正整数/);
  await assert.rejects(
    listSiteSimilarityGroups("scan-1", { page: 1, cursor: "next" }),
    /不能同时使用/,
  );
  await assert.rejects(
    listSiteSimilarityGroups("scan-1", { kind: "invalid" as "all" }),
    /分区无效/,
  );
  await assert.rejects(
    selectRecommendedSiteSimilarityDecisions("scan-1", {
      kind: "invalid" as "all",
      expectedVersion: 1,
    }),
    /分区无效/,
  );
  await assert.rejects(
    selectRecommendedSiteSimilarityDecisions("scan-1", {
      kind: "all",
      expectedVersion: 0,
    }),
    /正整数/,
  );
  await assert.rejects(applySiteSimilarityScan("scan-1", 0), /正整数/);
  assert.equal(fetchCount, 0);
});

test("reclassification confirmation sends both immutable snapshots", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let requestBody: unknown;
  globalThis.fetch = async (_input, init) => {
    requestBody = JSON.parse(String(init?.body));
    return new Response(JSON.stringify({ status: "success" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  await confirmAgentReclassify({
    expectedCategories: { "category-1": "开发" },
    expectedVersions: { "site-1": 3 },
  });

  assert.deepEqual(requestBody, {
    expected_categories: { "category-1": "开发" },
    expected_versions: { "site-1": 3 },
  });
});

test("surfaces structured backend error codes to interaction logic", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => new Response(JSON.stringify({
    detail: {
      code: "duplicate_url",
      message: "该网址已存在于当前账号的网址库",
    },
  }), { status: 409, headers: { "Content-Type": "application/json" } });

  await assert.rejects(
    createLibrarySite({ name: "Duplicate", url: "https://developer.mozilla.org" }),
    (error: unknown) => {
      assert.ok(error instanceof LibraryApiError);
      assert.equal(error.status, 409);
      assert.equal(error.code, "duplicate_url");
      assert.equal(error.message, "该网址已存在于当前账号的网址库");
      return true;
    },
  );
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
  await assert.rejects(deleteLibrarySite("site-1", 0), /正整数/);
  assert.equal(fetchCount, 0);
});
