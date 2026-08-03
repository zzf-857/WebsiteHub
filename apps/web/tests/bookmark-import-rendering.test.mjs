import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const WORKSPACE = new URL(
  "../components/settings/bookmark-import-workspace.tsx",
  import.meta.url,
);

test("书签导入相似组使用数字分页并在决策后保留当前页", async () => {
  const source = await readFile(WORKSPACE, "utf8");

  assert.match(source, /agentResultPageTokens\(clusterPage\.totalPages, clusterPage\.page\)/u);
  assert.match(source, /\{ page: pageNumber \}/u);
  assert.match(source, /aria-current=\{token === clusterPage\.page \? "page" : undefined\}/u);
  assert.match(source, /共 \{clusterPage\.totalPages\} 页/u);
  assert.match(source, /key=\{cluster\.id\}/u);
  assert.doesNotMatch(source, /setClusterPageIndex|setClusterPageCursors/u);
  assert.doesNotMatch(source, /refreshClustersAfterDecision/u);
});
