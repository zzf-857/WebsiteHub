import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";

import {
  applyBookmarkImport,
  getBookmarkSimilarityClusters,
  keepOriginalBookmarkSimilarityClusters,
  setBookmarkSimilarityDecision,
} from "../lib/bookmark-client.ts";

import {
  BookmarkContractError,
  bookmarkCanonicalSourceLabel,
  bookmarkFailureLabel,
  bookmarkImportStateLabel,
  bookmarkSimilarityReasonLabel,
  isBookmarkImportPending,
  isBookmarkImportPreviewReady,
  normalizeBookmarkImportResult,
  normalizeBookmarkImportStatus,
  normalizeBookmarkPreviewSummary,
  normalizeBookmarkSimilarityClusterPage,
  normalizeBookmarkSimilarityDecisionResult,
  normalizeBookmarkSimilarityMemberPage,
} from "../lib/bookmark-contract.ts";

function previewResponse(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    job_id: "job-1",
    run_id: "run-1",
    job_version: 4,
    preview_version: 1,
    source_sequence_count: 2909,
    folder_count: 368,
    occurrence_count: 2541,
    candidate_count: 2024,
    occurrence_counts: { accepted: 2535, invalid: 0, unsupported: 6 },
    duplicate_occurrence_count: 511,
    candidate_action_counts: {
      create: 2024,
      skip_existing: 0,
      merge_missing_metadata: 0,
      reject: 0,
      needs_review: 0,
    },
    metadata_only_candidate_count: 2,
    sensitive_candidate_count: 7,
    decision_version: 1,
    similarity_cluster_count: 2,
    similarity_candidate_count: 7,
    similarity_decision_counts: {
      unresolved: 1,
      merge_to_homepage: 1,
      keep_originals: 0,
    },
    selected_merge_reduction_count: 3,
    projected_create_count: 2021,
    ...overrides,
  };
}

function similarityMember(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    candidate_id: "candidate-1",
    title: "Example Docs",
    display_url: "https://example.com/docs?token=%5Bhidden%5D",
    occurrence_count: 2,
    first_source_sequence: 10,
    is_canonical: false,
    ...overrides,
  };
}

function similarityCluster(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "cluster-1",
    display_host: "example.com",
    confidence: "high",
    reason_codes: ["same_site_authority", "homepage_and_subpages"],
    candidate_count: 4,
    occurrence_count: 5,
    first_source_sequence: 8,
    decision: null,
    canonical: {
      candidate_id: null,
      url: "https://example.com/",
      title: "Example",
      source: "derived_origin_root",
    },
    sample_members: [similarityMember()],
    has_more_members: true,
    ...overrides,
  };
}

function similarityClusterPage(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    items: [similarityCluster()],
    next_cursor: "cursor-2",
    page: 1,
    page_size: 1,
    total_count: 2,
    total_pages: 2,
    decision_version: 3,
    ...overrides,
  };
}

type FetchCall = { input: string; init: RequestInit };

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function withMockFetch(t: TestContext, responses: Response[]): FetchCall[] {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ input: String(input), init: init ?? {} });
    const response = responses.shift();
    if (!response) throw new Error("测试未预置足够的模拟响应");
    return response;
  }) as typeof fetch;
  t.after(() => {
    globalThis.fetch = original;
  });
  return calls;
}

test("预览摘要归一化保留决定导入与否所需的全部计数", () => {
  const preview = normalizeBookmarkPreviewSummary(previewResponse());
  assert.equal(preview.jobId, "job-1");
  assert.equal(preview.jobVersion, 4);
  assert.equal(preview.folderCount, 368);
  assert.equal(preview.occurrenceCount, 2541);
  assert.equal(preview.candidateCount, 2024);
  assert.equal(preview.duplicateOccurrenceCount, 511);
  assert.equal(preview.sensitiveCandidateCount, 7);
  assert.equal(preview.decisionVersion, 1);
  assert.equal(preview.similarityClusterCount, 2);
  assert.equal(preview.similarityCandidateCount, 7);
  assert.deepEqual(preview.similarityDecisions, {
    unresolved: 1,
    mergeToHomepage: 1,
    keepOriginals: 0,
  });
  assert.equal(preview.selectedMergeReductionCount, 3);
  assert.equal(preview.projectedCreateCount, 2021);
  assert.equal(preview.actions.create, 2024);
  assert.equal(preview.actions.needsReview, 0);
});

test("计数为负或缺失时归一化抛错，不让半截数据进 state", () => {
  assert.throws(
    () => normalizeBookmarkPreviewSummary(previewResponse({ candidate_count: -1 })),
    BookmarkContractError,
  );
  assert.throws(
    () => normalizeBookmarkPreviewSummary(previewResponse({ candidate_action_counts: {} })),
    BookmarkContractError,
  );
  // job_version 是乐观锁令牌，0 不是合法版本。
  assert.throws(
    () => normalizeBookmarkPreviewSummary(previewResponse({ job_version: 0 })),
    BookmarkContractError,
  );
  assert.throws(
    () => normalizeBookmarkPreviewSummary(previewResponse({ decision_version: 0 })),
    BookmarkContractError,
  );
  assert.throws(
    () => normalizeBookmarkPreviewSummary(previewResponse({ similarity_decision_counts: {} })),
    BookmarkContractError,
  );
});

test("相似组分页只接收完整枚举、脱敏 URL 与版本游标", () => {
  const page = normalizeBookmarkSimilarityClusterPage(similarityClusterPage());
  assert.equal(page.decisionVersion, 3);
  assert.equal(page.nextCursor, "cursor-2");
  assert.equal(page.page, 1);
  assert.equal(page.pageSize, 1);
  assert.equal(page.totalCount, 2);
  assert.equal(page.totalPages, 2);
  assert.equal(page.items[0].displayHost, "example.com");
  assert.equal(page.items[0].decision, null);
  assert.equal(page.items[0].canonical.source, "derived_origin_root");
  assert.equal(page.items[0].sampleMembers[0].displayUrl.includes("%5Bhidden%5D"), true);
  assert.equal(page.items[0].hasMoreMembers, true);

  assert.throws(
    () => normalizeBookmarkSimilarityClusterPage(similarityClusterPage({
      items: [similarityCluster({ confidence: "certain" })],
    })),
    BookmarkContractError,
  );
  assert.throws(
    () => normalizeBookmarkSimilarityClusterPage(similarityClusterPage({ next_cursor: 42 })),
    BookmarkContractError,
  );
  assert.throws(
    () => normalizeBookmarkSimilarityClusterPage(similarityClusterPage({ total_pages: 3 })),
    BookmarkContractError,
  );
  assert.throws(
    () => normalizeBookmarkSimilarityClusterPage(similarityClusterPage({
      page: 2,
      next_cursor: "impossible-on-last-page",
    })),
    BookmarkContractError,
  );
});

test("相似组成员分页和决策结果保留乐观锁所需字段", () => {
  const members = normalizeBookmarkSimilarityMemberPage({
    items: [similarityMember({ is_canonical: true })],
    next_cursor: null,
    decision_version: 4,
  });
  assert.equal(members.items[0].isCanonical, true);
  assert.equal(members.nextCursor, null);
  assert.equal(members.decisionVersion, 4);

  const decision = normalizeBookmarkSimilarityDecisionResult({
    job_id: "job-1",
    run_id: "run-1",
    job_version: 4,
    decision_version: 5,
    similarity_decision_counts: {
      unresolved: 0,
      merge_to_homepage: 1,
      keep_originals: 1,
    },
    selected_merge_reduction_count: 3,
    projected_create_count: 2021,
  });
  assert.equal(decision.decisionVersion, 5);
  assert.equal(decision.similarityDecisions.unresolved, 0);
  assert.equal(decision.selectedMergeReductionCount, 3);
});

test("相似组证据与推荐主页来源都有稳定中文文案", () => {
  assert.equal(bookmarkSimilarityReasonLabel("homepage_and_subpages"), "主页与子页面");
  assert.equal(bookmarkSimilarityReasonLabel("future_reason"), "同站点相似页面");
  assert.equal(bookmarkCanonicalSourceLabel("existing_library"), "网址库中已有的推荐主页");
});

test("相似组请求和最终 apply 都提交双版本乐观锁", async (t) => {
  const decisionResponse = {
    job_id: "job/1",
    run_id: "run-1",
    job_version: 4,
    decision_version: 5,
    similarity_decision_counts: {
      unresolved: 0,
      merge_to_homepage: 1,
      keep_originals: 1,
    },
    selected_merge_reduction_count: 3,
    projected_create_count: 2021,
  };
  const calls = withMockFetch(t, [
    jsonResponse(similarityClusterPage()),
    jsonResponse(decisionResponse),
    jsonResponse({ ...decisionResponse, decision_version: 6 }),
    jsonResponse({
      job_id: "job/1",
      state: "completed",
      job_version: 5,
      total_candidates: 2024,
      created: 2021,
      skipped_existing: 0,
      skipped_needs_review: 0,
      merged_candidates: 3,
      failed: 0,
    }),
  ]);

  await getBookmarkSimilarityClusters("job/1", { cursor: "cursor +", limit: 999 });
  await setBookmarkSimilarityDecision("job/1", "cluster/1", {
    expectedJobVersion: 4,
    expectedDecisionVersion: 3,
    decision: "merge_to_homepage",
  });
  await keepOriginalBookmarkSimilarityClusters("job/1", {
    expectedJobVersion: 4,
    expectedDecisionVersion: 5,
  });
  await applyBookmarkImport("job/1", 4, 6);

  assert.equal(
    calls[0].input,
    "/api/backend/bookmark-imports/job%2F1/preview/similarity-clusters?limit=50&cursor=cursor+%2B",
  );
  assert.equal(calls[0].init.credentials, "include");
  assert.equal(calls[1].init.method, "PUT");
  assert.equal(
    calls[1].input,
    "/api/backend/bookmark-imports/job%2F1/preview/similarity-clusters/cluster%2F1/decision",
  );
  assert.deepEqual(JSON.parse(String(calls[1].init.body)), {
    expected_job_version: 4,
    expected_decision_version: 3,
    decision: "merge_to_homepage",
  });
  assert.deepEqual(JSON.parse(String(calls[2].init.body)), {
    expected_job_version: 4,
    expected_decision_version: 5,
    decision: "keep_originals",
  });
  assert.deepEqual(JSON.parse(String(calls[3].init.body)), {
    expected_job_version: 4,
    expected_decision_version: 6,
  });
});

test("相似组页码请求支持直接跳转并拒绝与游标混用", async (t) => {
  const calls = withMockFetch(t, [
    jsonResponse(similarityClusterPage({
      next_cursor: null,
      page: 2,
      page_size: 20,
      total_count: 21,
      total_pages: 2,
    })),
  ]);

  const page = await getBookmarkSimilarityClusters("job/1", { page: 2 });

  assert.equal(page.page, 2);
  assert.equal(page.totalPages, 2);
  assert.equal(
    calls[0].input,
    "/api/backend/bookmark-imports/job%2F1/preview/similarity-clusters?limit=20&page=2",
  );
  await assert.rejects(
    getBookmarkSimilarityClusters("job/1", { cursor: "cursor-1", page: 2 }),
    /页码和游标不能同时使用/u,
  );
  await assert.rejects(
    getBookmarkSimilarityClusters("job/1", { page: 0 }),
    /页码必须是正整数/u,
  );
});

test("轮询判定：只有推进中的状态才继续轮询", () => {
  for (const state of ["receiving", "queued_parse", "parsing", "committing"]) {
    assert.equal(isBookmarkImportPending(state), true, state);
  }
  // 终态与待确认态都必须停下来，否则会一直打接口。
  for (const state of ["parse_preview_ready", "completed", "failed", "cancelled", "expired"]) {
    assert.equal(isBookmarkImportPending(state), false, state);
  }
  assert.equal(isBookmarkImportPreviewReady("parse_preview_ready"), true);
  assert.equal(isBookmarkImportPreviewReady("final_preview_ready"), true);
  assert.equal(isBookmarkImportPreviewReady("completed"), false);
});

test("状态与失败原因都有中文文案，不会把裸 code 甩给用户", () => {
  assert.equal(bookmarkImportStateLabel("parsing"), "正在解析");
  assert.equal(bookmarkImportStateLabel("parse_preview_ready"), "解析完成，待确认");
  assert.equal(bookmarkFailureLabel("invalid_bookmark_file"), "文件不是可识别的书签导出，请确认导出格式");
  // 未知 code 也要给人话，而不是原样透出。
  assert.equal(bookmarkFailureLabel("some_new_code"), "导入失败，请重试");
  assert.equal(bookmarkFailureLabel(null), null);
});

test("状态归一化把进度与失败原因都带出来", () => {
  const status = normalizeBookmarkImportStatus({
    job_id: "job-1",
    state: "parsing",
    job_version: 2,
    preview_version: 0,
    progress: { completed: 500, total: 2909 },
    failure_code: null,
    created_at: "2026-07-26T14:00:00Z",
    updated_at: "2026-07-26T14:00:01Z",
    completed_at: null,
  });
  assert.equal(status.state, "parsing");
  assert.deepEqual(status.progress, { completed: 500, total: 2909 });
  assert.equal(status.failureCode, null);
});

test("导入结果归一化覆盖幂等重放那一趟（created 0 / 全部跳过）", () => {
  const replay = normalizeBookmarkImportResult({
    job_id: "job-1",
    state: "completed",
    job_version: 6,
    total_candidates: 2024,
    created: 0,
    skipped_existing: 2024,
    skipped_needs_review: 0,
    merged_candidates: 0,
    failed: 0,
  });
  assert.equal(replay.created, 0);
  assert.equal(replay.skippedExisting, 2024);
  assert.equal(replay.mergedCandidates, 0);
  assert.equal(replay.failed, 0);
});
