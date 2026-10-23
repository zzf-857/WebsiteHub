import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  formatFolderPath,
  getSiteImportOrigins,
  importOriginsSummary,
  normalizeSiteImportOrigins,
  ROOT_FOLDER_LABEL,
} from "../lib/import-origins.ts";
import { LibraryApiError } from "../lib/library-client.ts";

const ROOT = new URL("../", import.meta.url);
const read = (path: string) => readFile(new URL(path, ROOT), "utf8");

test("normalizes the import-origins payload defensively", () => {
  assert.deepEqual(
    normalizeSiteImportOrigins({
      site_id: "s",
      occurrence_count: 3,
      folder_paths: [["书签栏", "工具"], [], ["坏", 1], "x"],
      truncated: false,
    }),
    { occurrenceCount: 3, folderPaths: [["书签栏", "工具"], [], ["坏"]], truncated: false },
  );
  assert.deepEqual(normalizeSiteImportOrigins(null), {
    occurrenceCount: 0,
    folderPaths: [],
    truncated: false,
  });
});

test("formats folder paths and names the file root", () => {
  assert.equal(formatFolderPath(["书签栏", " 工具 "]), "书签栏 / 工具");
  assert.equal(formatFolderPath([]), ROOT_FOLDER_LABEL);
});

test("the compact line lists every origin up to two, then folds the rest", () => {
  assert.equal(importOriginsSummary({ occurrenceCount: 0, folderPaths: [], truncated: false }), null);
  assert.deepEqual(
    importOriginsSummary({ occurrenceCount: 1, folderPaths: [["书签栏", "AI"]], truncated: false }),
    { text: "来自书签：书签栏 / AI", full: "书签栏 / AI" },
  );
  const many = importOriginsSummary({
    occurrenceCount: 4,
    folderPaths: [["A"], ["A", "B"], ["C"], []],
    truncated: false,
  });
  assert.equal(many?.text, "来自书签：A、A / B 等 4 个目录");
  assert.equal(many?.full, `A\nA / B\nC\n${ROOT_FOLDER_LABEL}`);
  const truncated = importOriginsSummary({
    occurrenceCount: 30,
    folderPaths: [["A"]],
    truncated: true,
  });
  assert.equal(truncated?.text, "来自书签：A 等 1+ 个目录");
});

test("fetches the account-scoped read endpoint and surfaces API errors", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  try {
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return new Response(
        JSON.stringify({ site_id: "a b", occurrence_count: 1, folder_paths: [["X"]], truncated: false }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as typeof fetch;
    const origins = await getSiteImportOrigins("a b");
    assert.deepEqual(calls, ["/api/backend/library/sites/a%20b/import-origins"]);
    assert.deepEqual(origins.folderPaths, [["X"]]);

    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ detail: { code: "not_found", message: "网站不存在" } }), {
        status: 404,
        headers: { "content-type": "application/json" },
      })) as typeof fetch;
    await assert.rejects(getSiteImportOrigins("missing"), (error: unknown) => {
      assert.ok(error instanceof LibraryApiError);
      assert.equal(error.status, 404);
      return true;
    });
    await assert.rejects(getSiteImportOrigins("  "), TypeError);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("the detail page renders the read-only origin line from its own component", async () => {
  const line = await read("components/library/detail/import-origins-line.tsx");
  const page = await read("components/library/detail/site-detail-page.tsx");
  assert.match(page, /<ImportOriginsLine siteId=\{site\.id\} \/>/u);
  assert.match(line, /importOriginsSummary\(/u);
  assert.match(line, /from "lucide-react"/u);
  // 只读：不发写请求，失败/无来源时整行不出现
  assert.doesNotMatch(line, /method:/u);
  assert.match(line, /return null;/u);
  assert.doesNotMatch(line, /#[0-9a-fA-F]{3,8}\b/u);
});
