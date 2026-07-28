import assert from "node:assert/strict";
import test from "node:test";

import {
  assertSpaceCreateInput,
  assertSpaceMemberAddInput,
  assertSpaceReorderInput,
  assertSpaceUpdateInput,
  MAX_SPACE_REORDER_MEMBER_COUNT,
  MAX_SPACE_SITE_ID_LENGTH,
  normalizeSpaceDeletePreview,
  normalizeSpaceDeleteResult,
  normalizeSpaceDetail,
  normalizeSpaceMemberAddResult,
  normalizeSpaceMemberDeleteResult,
  normalizeSpacePage,
  SpaceContractError,
  spaceErrorDetails,
  spaceErrorMessage,
} from "../lib/space-contract.ts";

const space = {
  id: "space-1",
  name: "Research",
  member_count: 2,
  version: 4,
  created_at: "2026-07-25T10:00:00Z",
  updated_at: "2026-07-26T10:00:00Z",
};

const member = {
  site: {
    id: "site-1",
    name: "MDN",
    original_url: "https://developer.mozilla.org/",
    identity_url: "https://developer.mozilla.org",
    summary: "提供权威且系统的 Web 开发标准参考资料",
    description: "",
    favicon_url: "https://developer.mozilla.org/favicon.ico",
    pinned: true,
    version: 3,
  },
  position: 0,
  added_at: "2026-07-26T09:00:00Z",
};

test("normalizes strict space pages and paginated member details", () => {
  assert.deepEqual(
    normalizeSpacePage({
      items: [space],
      next_cursor: "next spaces",
      aggregate: { total_count: 7 },
    }),
    {
      items: [{
        id: "space-1",
        name: "Research",
        memberCount: 2,
        version: 4,
        createdAt: "2026-07-25T10:00:00Z",
        updatedAt: "2026-07-26T10:00:00Z",
      }],
      nextCursor: "next spaces",
      aggregate: { totalCount: 7 },
    },
  );

  const detail = normalizeSpaceDetail({ ...space, members: [member], next_cursor: null });
  assert.equal(detail.members[0]?.site.description, "");
  assert.equal(detail.members[0]?.site.originalUrl, "https://developer.mozilla.org/");
  assert.equal(detail.members[0]?.position, 0);
  assert.equal(detail.nextCursor, null);
});

test("normalizes member mutation and non-destructive delete responses", () => {
  const added = normalizeSpaceMemberAddResult({
    space: { ...space, member_count: 3, version: 5 },
    member,
  });
  assert.equal(added.space.version, 5);
  assert.equal(added.member.site.id, "site-1");

  assert.deepEqual(
    normalizeSpaceMemberDeleteResult({
      message: "网站已从 Space 移除",
      space_id: "space-1",
      site_id: "site-1",
      member_count: 1,
      version: 6,
    }),
    {
      message: "网站已从 Space 移除",
      spaceId: "space-1",
      siteId: "site-1",
      memberCount: 1,
      version: 6,
    },
  );

  assert.equal(
    normalizeSpaceDeletePreview({ space, affected_site_count: 2 }).affectedSiteCount,
    2,
  );
  assert.deepEqual(
    normalizeSpaceDeleteResult({
      message: "Space 已删除",
      space_id: "space-1",
      unlinked_site_count: 2,
    }),
    { message: "Space 已删除", spaceId: "space-1", unlinkedSiteCount: 2 },
  );
});

test("rejects malformed response fields before they enter UI state", () => {
  assert.throws(
    () => normalizeSpacePage({ items: [{ ...space, version: 0 }], next_cursor: null, aggregate: { total_count: 1 } }),
    (error: unknown) => error instanceof SpaceContractError && /version/.test(error.message),
  );
  assert.throws(
    () => normalizeSpaceDetail({ ...space, members: [{ ...member, position: -1 }], next_cursor: null }),
    /position/,
  );
  assert.throws(
    () => normalizeSpaceDetail({
      ...space,
      members: [{ ...member, site: { ...member.site, original_url: "javascript:alert(1)" } }],
      next_cursor: null,
    }),
    /HTTP\(S\) URL/,
  );
  assert.throws(
    () => normalizeSpaceDetail({
      ...space,
      members: [{ ...member, site: { ...member.site, favicon_url: "data:image/svg+xml,test" } }],
      next_cursor: null,
    }),
    /favicon_url.*HTTP\(S\) URL/,
  );
  assert.equal(
    normalizeSpaceDetail({
      ...space,
      members: [{ ...member, site: { ...member.site, favicon_url: null } }],
      next_cursor: null,
    }).members[0]?.site.faviconUrl,
    null,
  );
  assert.throws(
    () => normalizeSpacePage({ items: [], next_cursor: null, aggregate: {} }),
    /total_count/,
  );
});

test("normalizes names and validates every versioned mutation input", () => {
  assert.deepEqual(assertSpaceCreateInput({ name: "  Ｒｅｓｅａｒｃｈ\n  Notes  " }), {
    name: "Research Notes",
  });
  assert.deepEqual(assertSpaceUpdateInput({ expectedVersion: 2, name: "Product   launch" }), {
    expectedVersion: 2,
    name: "Product launch",
  });
  assert.deepEqual(assertSpaceMemberAddInput({ expectedVersion: 3, siteId: " site-1 " }), {
    expectedVersion: 3,
    siteId: "site-1",
  });
  assert.deepEqual(
    assertSpaceReorderInput({
      expectedVersion: 4,
      orderedSiteIds: ["site-2", " site-1 "],
      beforeSiteId: null,
    }),
    {
      expectedVersion: 4,
      orderedSiteIds: ["site-2", "site-1"],
      beforeSiteId: null,
    },
  );
});

test("enforces backend reorder limits, uniqueness and anchor semantics", () => {
  assert.throws(
    () => assertSpaceReorderInput({ expectedVersion: 1, orderedSiteIds: [] }),
    /至少需要一个成员/,
  );
  assert.throws(
    () => assertSpaceReorderInput({ expectedVersion: 1, orderedSiteIds: ["site-1", " site-1 "] }),
    /不能重复/,
  );
  assert.throws(
    () => assertSpaceReorderInput({
      expectedVersion: 1,
      orderedSiteIds: ["site-1"],
      beforeSiteId: "site-1",
    }),
    /定位成员/,
  );
  assert.throws(
    () => assertSpaceMemberAddInput({ expectedVersion: 1, siteId: "s".repeat(MAX_SPACE_SITE_ID_LENGTH + 1) }),
    new RegExp(String(MAX_SPACE_SITE_ID_LENGTH)),
  );
  assert.throws(
    () => assertSpaceReorderInput({
      expectedVersion: 1,
      orderedSiteIds: Array.from(
        { length: MAX_SPACE_REORDER_MEMBER_COUNT + 1 },
        (_, index) => `site-${index}`,
      ),
    }),
    new RegExp(String(MAX_SPACE_REORDER_MEMBER_COUNT)),
  );
  assert.throws(
    () => assertSpaceUpdateInput({ expectedVersion: 0, name: "Invalid" }),
    /正整数/,
  );
});

test("extracts structured backend errors without losing legacy or status fallbacks", () => {
  for (const code of [
    "version_conflict",
    "duplicate_name",
    "not_found",
    "member_exists",
    "member_not_found",
  ]) {
    assert.deepEqual(
      spaceErrorDetails(409, {
        detail: { code: `  ${code}  `, message: `  ${code} message  ` },
      }),
      { code, message: `${code} message` },
    );
  }

  assert.deepEqual(spaceErrorDetails(404, { detail: { code: "not_found" } }), {
    code: "not_found",
    message: "请求的 Space 或网站不存在",
  });
  assert.deepEqual(
    spaceErrorDetails(409, {
      code: "conflict",
      message: "top-level message",
      detail: { code: "version_conflict", message: "nested message" },
    }),
    { code: "version_conflict", message: "nested message" },
  );
  assert.equal(spaceErrorMessage(422, { detail: [{ msg: "排序成员不能重复" }] }), "排序成员不能重复");
  assert.equal(spaceErrorMessage(409, { detail: "Space 已被修改，请刷新后重试" }), "Space 已被修改，请刷新后重试");
  assert.equal(spaceErrorMessage(404, null), "请求的 Space 或网站不存在");
  assert.equal(spaceErrorMessage(503, null), "Space 服务暂时不可用，请稍后重试");
});
