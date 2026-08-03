import assert from "node:assert/strict";
import test from "node:test";

import {
  assertLibraryBulkDeleteItems,
  assertLibraryCategoryName,
  assertLibraryExpectedVersion,
  assertLibrarySiteCreateInput,
  assertLibrarySiteUpdateInput,
  assertLibraryTagName,
  libraryErrorDetails,
  libraryErrorMessage,
  LibraryContractError,
  MAX_LIBRARY_BULK_DELETE_SITES,
  normalizeCategoryDeletePreview,
  normalizeLibraryCategories,
  normalizeLibrarySite,
  normalizeLibrarySitePage,
  normalizeLibraryBulkDeleteResult,
  normalizeMetadataBackfillPlan,
  normalizeMetadataBackfillProgress,
  normalizeSiteSimilarityApply,
  normalizeSiteSimilarityDecision,
  normalizeSiteSimilarityGroupPage,
  normalizeSiteSimilarityRecommendedDecision,
  normalizeSiteSimilarityScan,
} from "../lib/library-contract.ts";

const category = { id: "category-1", name: "开发", is_default: true, icon: "Code", site_count: 2 };
const site = {
  id: "site-1",
  name: "MDN",
  original_url: "https://developer.mozilla.org/",
  identity_url: "https://developer.mozilla.org",
  summary: "提供权威且系统的 Web 开发标准参考资料",
  description: "Web documentation",
  favicon_url: "https://developer.mozilla.org/favicon.ico",
  preview_url: "https://developer.mozilla.org/social.png",
  category: { id: "category-1", name: "开发", is_default: true, icon: "Code" },
  tags: [{ id: "tag-1", name: "文档" }],
  pinned: true,
  source: "manual",
  analysis_status: "not_analyzed",
  analysis_phase: null,
  version: 3,
  created_at: "2026-07-25T10:00:00Z",
  updated_at: "2026-07-26T10:00:00Z",
};

test("normalizes strict site and paginated list contracts", () => {
  assert.deepEqual(normalizeLibrarySite(site), {
    id: "site-1",
    name: "MDN",
    originalUrl: "https://developer.mozilla.org/",
    identityUrl: "https://developer.mozilla.org",
    summary: "提供权威且系统的 Web 开发标准参考资料",
    description: "Web documentation",
    faviconUrl: "https://developer.mozilla.org/favicon.ico",
    previewUrl: "https://developer.mozilla.org/social.png",
    category: { id: "category-1", name: "开发", isDefault: true, icon: "Code" },
    tags: [{ id: "tag-1", name: "文档" }],
    pinned: true,
    source: "manual",
    analysisStatus: "not_analyzed",
    analysisPhase: null,
    version: 3,
    createdAt: "2026-07-25T10:00:00Z",
    updatedAt: "2026-07-26T10:00:00Z",
  });

  const page = normalizeLibrarySitePage({
    items: [site],
    next_cursor: "next-page",
    aggregate: { matched_count: 12, pinned_count: 4 },
  });
  assert.equal(page.items[0]?.id, "site-1");
  assert.equal(page.nextCursor, "next-page");
  assert.deepEqual(page.aggregate, { matchedCount: 12, pinnedCount: 4 });
});

test("accepts the collection envelope and the explicit category delete preview", () => {
  assert.deepEqual(normalizeLibraryCategories({ items: [category] }), [
    { id: "category-1", name: "开发", isDefault: true, icon: "Code", siteCount: 2 },
  ]);
  assert.deepEqual(
    normalizeCategoryDeletePreview({
      category,
      affected_site_count: 2,
      replacement_category: { ...category, id: "category-2", name: "未分类", site_count: 0 },
    }),
    {
      category: { id: "category-1", name: "开发", isDefault: true, icon: "Code", siteCount: 2 },
      affectedSiteCount: 2,
      replacementCategory: {
        id: "category-2",
        name: "未分类",
        isDefault: true,
        icon: "Code",
        siteCount: 0,
      },
    },
  );
});

test("rejects malformed nested values instead of leaking them into UI state", () => {
  assert.throws(
    () => normalizeLibrarySite({ ...site, pinned: "yes" }),
    (error: unknown) => error instanceof LibraryContractError && /site\.pinned/.test(error.message),
  );
  assert.throws(
    () => normalizeLibrarySite({ ...site, original_url: "javascript:alert(1)" }),
    /HTTP\(S\) URL/,
  );
  assert.throws(
    () => normalizeLibrarySitePage({ items: [], next_cursor: null, aggregate: {} }),
    /matched_count/,
  );
});

test("accepts only backend-supported site source and analysis status values", () => {
  for (const source of ["manual", "agent", "browser_import", "backup"] as const) {
    assert.equal(normalizeLibrarySite({ ...site, source }).source, source);
  }
  for (const analysisStatus of ["not_analyzed", "pending", "complete", "failed", "limited"] as const) {
    assert.equal(
      normalizeLibrarySite({ ...site, analysis_status: analysisStatus }).analysisStatus,
      analysisStatus,
    );
  }
  assert.throws(() => normalizeLibrarySite({ ...site, source: "web_import" }), /site\.source/);
  assert.throws(() => normalizeLibrarySite({ ...site, analysis_status: "not_requested" }), /site\.analysis_status/);
  for (const analysisPhase of [
    "fetching_page",
    "preparing_evidence",
    "waiting_model",
    "calling_model",
    "saving_result",
  ] as const) {
    assert.equal(
      normalizeLibrarySite({ ...site, analysis_phase: analysisPhase }).analysisPhase,
      analysisPhase,
    );
  }
  assert.throws(() => normalizeLibrarySite({ ...site, analysis_phase: "thinking" }), /site\.analysis_phase/);
});

test("normalizes create descriptions and preserves explicit null category updates", () => {
  assert.equal(
    assertLibrarySiteCreateInput({ name: "MDN", url: "https://developer.mozilla.org", description: null }).description,
    undefined,
  );
  assert.equal(
    assertLibrarySiteCreateInput({ name: "MDN", url: "https://developer.mozilla.org", description: "   " }).description,
    undefined,
  );
  assert.deepEqual(assertLibrarySiteUpdateInput({ expectedVersion: 3, categoryId: null }), {
    expectedVersion: 3,
    categoryId: null,
  });
  assert.deepEqual(assertLibrarySiteUpdateInput({ expectedVersion: 3, pinned: false }), {
    expectedVersion: 3,
    pinned: false,
  });
});

test("enforces backend name length limits", () => {
  assert.equal(assertLibrarySiteCreateInput({
    name: "s".repeat(160),
    url: "https://example.com",
  }).name.length, 160);
  assert.equal(assertLibraryCategoryName("c".repeat(80)).length, 80);
  assert.equal(assertLibraryTagName("t".repeat(40)).length, 40);

  assert.throws(
    () => assertLibrarySiteUpdateInput({ expectedVersion: 1, name: "s".repeat(161) }),
    /160/,
  );
  assert.throws(() => assertLibraryCategoryName("c".repeat(81)), /80/);
  assert.throws(() => assertLibraryTagName("t".repeat(41)), /40/);
});

test("parses structured library error codes without losing readable messages", () => {
  for (const code of ["version_conflict", "duplicate_url", "not_found"]) {
    assert.deepEqual(
      libraryErrorDetails(409, {
        detail: { code: `  ${code}  `, message: `  ${code} message  ` },
      }),
      { code, message: `${code} message` },
    );
  }

  assert.deepEqual(libraryErrorDetails(404, { detail: { code: "not_found" } }), {
    code: "not_found",
    message: "请求的网址库内容不存在",
  });
  assert.equal(libraryErrorMessage(422, { detail: [{ msg: "Invalid field" }] }), "Invalid field");
});

test("validates optimistic concurrency versions independently of update payloads", () => {
  assert.equal(assertLibraryExpectedVersion(4), 4);
  assert.throws(() => assertLibraryExpectedVersion(0), /正整数/);
  assert.throws(() => assertLibraryExpectedVersion(true), /正整数/);
});

test("validates and normalizes the versioned bulk-delete contract", () => {
  assert.deepEqual(assertLibraryBulkDeleteItems([
    { siteId: " site-1 ", expectedVersion: 2 },
    { siteId: "site-2", expectedVersion: 4 },
  ]), [
    { siteId: "site-1", expectedVersion: 2 },
    { siteId: "site-2", expectedVersion: 4 },
  ]);
  assert.deepEqual(normalizeLibraryBulkDeleteResult({
    deleted_site_ids: ["site-1", "site-2"],
  }), { deletedSiteIds: ["site-1", "site-2"] });

  assert.throws(() => assertLibraryBulkDeleteItems([]), /至少需要一个/);
  assert.throws(() => assertLibraryBulkDeleteItems([
    { siteId: "site-1", expectedVersion: 1 },
    { siteId: "site-1", expectedVersion: 2 },
  ]), /重复网站/);
  assert.throws(
    () => assertLibraryBulkDeleteItems(Array.from(
      { length: MAX_LIBRARY_BULK_DELETE_SITES + 1 },
      (_, index) => ({ siteId: `site-${index}`, expectedVersion: 1 }),
    )),
    /单次最多删除/,
  );
  assert.throws(
    () => normalizeLibraryBulkDeleteResult({ deleted_site_ids: ["site-1", "site-1"] }),
    /重复网站/,
  );
});

test("normalizes the complete site-similarity review contract", () => {
  const scan = normalizeSiteSimilarityScan({
    id: "scan-1",
    status: "ready",
    ruleset_version: "library-site-similarity.v1",
    source_site_count: 20,
    group_count: 2,
    duplicate_group_count: 1,
    same_site_group_count: 1,
    candidate_site_count: 5,
    selected_group_count: 0,
    selected_delete_count: 0,
    version: 1,
    decision_version: 1,
    created_at: "2026-07-31T08:00:00Z",
    applied_at: null,
  });
  assert.equal(scan.runId, "scan-1");
  assert.equal(scan.groupCount, 2);

  const page = normalizeSiteSimilarityGroupPage({
    items: [{
      id: "group-1",
      kind: "duplicate",
      site_key: "example.com",
      display_host: "example.com",
      member_count: 3,
      recommended_site_id: "site-1",
      keep_site_ids: ["site-1", "site-2"],
      members: [
        { ...site, is_recommended: true },
        {
          ...site,
          id: "site-2",
          name: "MDN mirror",
          original_url: "http://www.developer.mozilla.org/",
          identity_url: "http://www.developer.mozilla.org/",
          is_recommended: false,
        },
        {
          ...site,
          id: "site-3",
          name: "MDN guide",
          original_url: "https://developer.mozilla.org/guide",
          identity_url: "https://developer.mozilla.org/guide",
          is_recommended: false,
        },
      ],
    }],
    next_cursor: "next",
    page: 1,
    page_size: 1,
    total_count: 2,
    total_pages: 2,
    decision_version: 1,
  });
  assert.equal(page.items[0]?.members.length, 3);
  assert.equal(page.items[0]?.recommendedSiteId, "site-1");
  assert.deepEqual(page.items[0]?.keepSiteIds, ["site-1", "site-2"]);
  assert.deepEqual(
    {
      page: page.page,
      pageSize: page.pageSize,
      totalCount: page.totalCount,
      totalPages: page.totalPages,
    },
    { page: 1, pageSize: 1, totalCount: 2, totalPages: 2 },
  );

  assert.deepEqual(normalizeSiteSimilarityDecision({
    group_id: "group-1",
    keep_site_ids: ["site-1", "site-2"],
    decision_version: 2,
    selected_group_count: 1,
    selected_delete_count: 1,
  }), {
    groupId: "group-1",
    keepSiteIds: ["site-1", "site-2"],
    decisionVersion: 2,
    selectedGroupCount: 1,
    selectedDeleteCount: 1,
  });
  assert.deepEqual(normalizeSiteSimilarityRecommendedDecision({
    kind: "duplicate",
    matched_group_count: 1,
    updated_group_count: 1,
    decision_version: 3,
    selected_group_count: 2,
    selected_delete_count: 3,
  }), {
    kind: "duplicate",
    matchedGroupCount: 1,
    updatedGroupCount: 1,
    decisionVersion: 3,
    selectedGroupCount: 2,
    selectedDeleteCount: 3,
  });
  assert.deepEqual(normalizeSiteSimilarityApply({
    id: "scan-1",
    status: "applied",
    decision_version: 3,
    merged_group_count: 1,
    deleted_site_count: 1,
    kept_site_count: 1,
    deleted_site_ids: ["site-2"],
    kept_site_ids: ["site-1"],
    applied_at: "2026-07-31T08:10:00Z",
  }).deletedSiteIds, ["site-2"]);
});

test("rejects inconsistent site-similarity snapshots", () => {
  assert.throws(() => normalizeSiteSimilarityScan({
    id: "scan-1",
    status: "ready",
    ruleset_version: "v1",
    source_site_count: 2,
    group_count: 2,
    duplicate_group_count: 1,
    same_site_group_count: 0,
    candidate_site_count: 2,
    selected_group_count: 0,
    selected_delete_count: 0,
    version: 1,
    decision_version: 1,
    created_at: "2026-07-31T08:00:00Z",
    applied_at: null,
  }), /分组计数不一致/);
  assert.throws(() => normalizeSiteSimilarityGroupPage({
    items: [{
      id: "group-1",
      kind: "duplicate",
      site_key: "example.com",
      display_host: "example.com",
      member_count: 2,
      recommended_site_id: "missing",
      keep_site_ids: [],
      members: [
        { ...site, is_recommended: true },
        { ...site, id: "site-2", is_recommended: false },
      ],
    }],
    next_cursor: null,
    page: 1,
    page_size: 12,
    total_count: 1,
    total_pages: 1,
    decision_version: 1,
  }), /推荐成员不一致/);

  assert.deepEqual(normalizeSiteSimilarityGroupPage({
    items: [],
    next_cursor: null,
    page: 1,
    page_size: 12,
    total_count: 0,
    total_pages: 0,
    decision_version: 2,
  }), {
    items: [],
    nextCursor: null,
    page: 1,
    pageSize: 12,
    totalCount: 0,
    totalPages: 0,
    decisionVersion: 2,
  });

  assert.throws(() => normalizeSiteSimilarityGroupPage({
    items: [],
    next_cursor: null,
    page: 2,
    page_size: 12,
    total_count: 12,
    total_pages: 1,
    decision_version: 1,
  }), /页码元数据不一致/);
  assert.throws(() => normalizeSiteSimilarityGroupPage({
    items: [],
    next_cursor: "unexpected",
    page: 1,
    page_size: 12,
    total_count: 0,
    total_pages: 0,
    decision_version: 1,
  }), /分页内容超出范围/);
  assert.throws(() => normalizeSiteSimilarityRecommendedDecision({
    kind: "same_site",
    matched_group_count: 2,
    updated_group_count: 3,
    decision_version: 2,
    selected_group_count: 3,
    selected_delete_count: 3,
  }), /不能超过/);
  assert.throws(() => normalizeSiteSimilarityRecommendedDecision({
    kind: "same_site",
    matched_group_count: 2,
    updated_group_count: 2,
    decision_version: 2,
    selected_group_count: 1,
    selected_delete_count: 1,
  }), /选择计数不一致/);
});

test("normalizes metadata backfill plans and rejects unsupported modes", () => {
  assert.deepEqual(normalizeMetadataBackfillPlan({
    mode: "full",
    requested_limit: 50,
    eligible_count: 87,
    selected_count: 50,
    llm_count: 42,
    max_limit: 100,
  }), {
    mode: "full",
    requestedLimit: 50,
    eligibleCount: 87,
    selectedCount: 50,
    llmCount: 42,
    maxLimit: 100,
  });

  assert.throws(
    () => normalizeMetadataBackfillPlan({
      mode: "automatic",
      requested_limit: 50,
      eligible_count: 87,
      selected_count: 50,
      llm_count: 42,
      max_limit: 100,
    }),
    (error: unknown) => error instanceof LibraryContractError && /plan\.mode/.test(error.message),
  );
  assert.throws(
    () => normalizeMetadataBackfillPlan({
      mode: "metadata",
      requested_limit: 200,
      eligible_count: 20,
      selected_count: 20,
      llm_count: 1,
      max_limit: 500,
    }),
    /不能包含 LLM/,
  );
});

test("normalizes metadata backfill progress and rejects unknown stop reasons", () => {
  const progress = {
    id: "run-1",
    mode: "full",
    status: "failed",
    stopped_early: true,
    stop_reason: "provider_rate_limited",
    provider_retry_at: "2026-07-31T08:30:00Z",
    total_count: 3,
    queued_count: 0,
    running_count: 0,
    completed_count: 3,
    complete_count: 1,
    limited_count: 0,
    failed_count: 2,
    skipped_count: 0,
  };

  assert.deepEqual(normalizeMetadataBackfillProgress(progress), {
    runId: "run-1",
    mode: "full",
    status: "failed",
    stoppedEarly: true,
    stopReason: "provider_rate_limited",
    providerRetryAt: "2026-07-31T08:30:00Z",
    totalCount: 3,
    queuedCount: 0,
    runningCount: 0,
    completedCount: 3,
    completeCount: 1,
    limitedCount: 0,
    failedCount: 2,
    skippedCount: 0,
  });
  assert.throws(
    () => normalizeMetadataBackfillProgress({ ...progress, mode: "legacy" }),
    (error: unknown) => error instanceof LibraryContractError && /backfill\.mode/.test(error.message),
  );
  assert.throws(
    () => normalizeMetadataBackfillProgress({ ...progress, stop_reason: "provider_timeout" }),
    (error: unknown) => error instanceof LibraryContractError && /stop_reason/.test(error.message),
  );
  assert.throws(
    () => normalizeMetadataBackfillProgress({ ...progress, provider_retry_at: "later" }),
    /有效日期/,
  );
});
