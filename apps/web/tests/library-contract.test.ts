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
