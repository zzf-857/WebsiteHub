import test from "node:test";
import assert from "node:assert/strict";

import {
  BookmarkContractError,
  bookmarkFailureLabel,
  bookmarkImportStateLabel,
  isBookmarkImportPending,
  isBookmarkImportPreviewReady,
  normalizeBookmarkImportResult,
  normalizeBookmarkImportStatus,
  normalizeBookmarkPreviewSummary,
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
    ...overrides,
  };
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
    failed: 0,
  });
  assert.equal(replay.created, 0);
  assert.equal(replay.skippedExisting, 2024);
  assert.equal(replay.failed, 0);
});
