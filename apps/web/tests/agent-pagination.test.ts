import assert from "node:assert/strict";
import test from "node:test";
import {
  AGENT_RESULT_PAGE_SIZE,
  agentResultPageCount,
  agentResultPageSlice,
  agentResultPageTokens,
} from "../lib/agent-pagination.ts";

test("splits complete Agent results into stable twelve-item pages", () => {
  const items = Array.from({ length: 87 }, (_, index) => `site-${index + 1}`);

  assert.equal(AGENT_RESULT_PAGE_SIZE, 12);
  assert.equal(agentResultPageCount(items.length), 8);
  assert.deepEqual(agentResultPageSlice(items, 1), {
    items: items.slice(0, 12),
    page: 1,
    startIndex: 0,
    pageCount: 8,
  });
  assert.deepEqual(agentResultPageSlice(items, 8), {
    items: items.slice(84),
    page: 8,
    startIndex: 84,
    pageCount: 8,
  });
});

test("clamps invalid and out-of-range page requests without dropping results", () => {
  const items = Array.from({ length: 13 }, (_, index) => index);

  assert.equal(agentResultPageSlice(items, -5).page, 1);
  assert.equal(agentResultPageSlice(items, 99).page, 2);
  assert.equal(agentResultPageSlice(items, Number.NaN).page, 1);
  assert.deepEqual(agentResultPageSlice([], 1), {
    items: [],
    page: 0,
    startIndex: 0,
    pageCount: 0,
  });
  assert.equal(agentResultPageCount(12), 1);
  assert.equal(agentResultPageCount(13), 2);
});

test("keeps page-number controls compact around the active page", () => {
  assert.deepEqual(agentResultPageTokens(0, 1), []);
  assert.deepEqual(agentResultPageTokens(5, 3), [1, 2, 3, 4, 5]);
  assert.deepEqual(agentResultPageTokens(12, 1), [1, 2, 3, 4, "ellipsis-end", 12]);
  assert.deepEqual(agentResultPageTokens(12, 6), [1, "ellipsis-start", 5, 6, 7, "ellipsis-end", 12]);
  assert.deepEqual(agentResultPageTokens(12, 12), [1, "ellipsis-start", 9, 10, 11, 12]);
});
