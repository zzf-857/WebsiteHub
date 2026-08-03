import assert from "node:assert/strict";
import test from "node:test";

import {
  SITE_ANALYSIS_PROGRESS_STEPS,
  siteAnalysisProgressRows,
} from "../lib/site-analysis-progress.ts";

test("maps a real backend phase to completed, current, and pending steps", () => {
  const rows = siteAnalysisProgressRows("calling_model");
  assert.deepEqual(
    rows.map((row) => row.state),
    ["done", "done", "done", "done", "current", "pending"],
  );
  assert.equal(rows[4]?.label, "正在补充公开资料并生成分类、标签与介绍");
});

test("keeps the queue as the only current step before a backend claim", () => {
  const rows = siteAnalysisProgressRows("queued");
  assert.equal(rows[0]?.state, "current");
  assert.equal(rows.filter((row) => row.state === "done").length, 0);
  assert.equal(rows.length, SITE_ANALYSIS_PROGRESS_STEPS.length);
});
