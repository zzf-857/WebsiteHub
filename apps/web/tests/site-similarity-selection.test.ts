import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeSiteSimilarityKeepSelection,
  siteSimilarityKeepAction,
  toggleSiteSimilarityKeepSelection,
} from "../lib/site-similarity-selection.ts";

const group = {
  members: [
    { id: "site-a" },
    { id: "site-b" },
    { id: "site-c" },
  ],
};

test("continuous card clicks toggle only that card and preserve other selections", () => {
  const selectedA = toggleSiteSimilarityKeepSelection(group, [], "site-a");
  assert.deepEqual(selectedA, ["site-a"]);

  const selectedAB = toggleSiteSimilarityKeepSelection(group, selectedA, "site-b");
  assert.deepEqual(selectedAB, ["site-a", "site-b"]);

  const selectedB = toggleSiteSimilarityKeepSelection(group, selectedAB, "site-a");
  assert.deepEqual(selectedB, ["site-b"]);
  assert.deepEqual(selectedA, ["site-a"], "计算下一次选择不能改写上一次状态");
});

test("the group action submits the complete ordered selection with the correct label", () => {
  assert.deepEqual(siteSimilarityKeepAction(group, ["site-c", "site-a"]), {
    label: "保留所选",
    keepSiteIds: ["site-a", "site-c"],
  });
  assert.deepEqual(siteSimilarityKeepAction(group, []), {
    label: "全部保留",
    keepSiteIds: [],
  });
});

test("selecting every card normalizes to the safe all-kept state", () => {
  assert.deepEqual(
    normalizeSiteSimilarityKeepSelection(group, ["site-a", "site-b", "site-c"]),
    [],
  );
  assert.deepEqual(
    toggleSiteSimilarityKeepSelection(group, ["site-a", "site-b"], "site-c"),
    [],
  );
});

test("selection normalization drops stale ids and rejects toggles outside the group", () => {
  assert.deepEqual(
    normalizeSiteSimilarityKeepSelection(group, ["stale", "site-b", "site-b"]),
    ["site-b"],
  );
  assert.throws(
    () => toggleSiteSimilarityKeepSelection(group, ["site-a"], "stale"),
    /当前相似网站分组/,
  );
});
