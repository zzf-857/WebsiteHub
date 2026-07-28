import assert from "node:assert/strict";
import test from "node:test";

import {
  addSpaceMember,
  buildSpaceDetailSearchParams,
  buildSpaceListSearchParams,
  createSpace,
  DEFAULT_SPACE_MEMBER_PAGE_SIZE,
  deleteSpace,
  getSpace,
  listSpaces,
  MAX_SPACE_PAGE_SIZE,
  previewSpaceDelete,
  removeSpaceMember,
  reorderSpaceMembers,
  SpaceApiError,
  updateSpace,
} from "../lib/space-client.ts";

const space = {
  id: "space-1",
  name: "Research",
  member_count: 1,
  version: 2,
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
    description: "Documentation",
    favicon_url: null,
    pinned: false,
    version: 1,
  },
  position: 0,
  added_at: "2026-07-26T09:00:00Z",
};

test("builds bounded list and member-detail queries", () => {
  const listParams = buildSpaceListSearchParams({
    sort: "name",
    direction: "asc",
    cursor: " cursor value ",
    limit: 999,
  });
  assert.equal(listParams.get("sort"), "name");
  assert.equal(listParams.get("direction"), "asc");
  assert.equal(listParams.get("cursor"), "cursor value");
  assert.equal(listParams.get("limit"), String(MAX_SPACE_PAGE_SIZE));

  const detailParams = buildSpaceDetailSearchParams();
  assert.equal(detailParams.get("limit"), String(DEFAULT_SPACE_MEMBER_PAGE_SIZE));
  assert.equal(detailParams.has("cursor"), false);
  assert.throws(
    () => buildSpaceListSearchParams({ sort: "invalid" as "name" }),
    /sort/,
  );
});

test("list and detail requests preserve cookies, signals, pagination and encoded IDs", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  const controller = new AbortController();
  globalThis.fetch = async (input, init) => {
    requests.push({ input: String(input), init });
    const payload = String(input).includes("space%2Fone")
      ? { ...space, members: [member], next_cursor: "member-next" }
      : { items: [space], next_cursor: null, aggregate: { total_count: 1 } };
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  };

  const page = await listSpaces({ sort: "created", limit: 5 }, controller.signal);
  const detail = await getSpace(" space/one ", { cursor: "next/member", limit: 10 }, controller.signal);

  assert.match(requests[0]?.input ?? "", /^\/api\/backend\/spaces\?/);
  assert.match(requests[0]?.input ?? "", /sort=created/);
  assert.equal(requests[0]?.init?.credentials, "include");
  assert.equal(requests[0]?.init?.cache, "no-store");
  assert.equal(requests[0]?.init?.signal, controller.signal);
  assert.equal(
    requests[1]?.input,
    "/api/backend/spaces/space%2Fone?cursor=next%2Fmember&limit=10",
  );
  assert.equal(page.aggregate.totalCount, 1);
  assert.equal(detail.members[0]?.site.name, "MDN");
});

test("serializes create, update, add and reorder requests to backend snake case", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  const controller = new AbortController();
  globalThis.fetch = async (input, init) => {
    requests.push({ input: String(input), init });
    const payload = String(input).endsWith("/members")
      ? { space: { ...space, version: 3 }, member }
      : { ...space, version: 3 };
    return new Response(JSON.stringify(payload), {
      status: String(input).endsWith("/members") ? 201 : 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  await createSpace({ name: "  Ｒｅｓｅａｒｃｈ   Notes " }, controller.signal);
  await updateSpace("space/one", { expectedVersion: 2, name: "New   name" }, controller.signal);
  await addSpaceMember(
    "space/one",
    { expectedVersion: 2, siteId: " site/one " },
    controller.signal,
  );
  await reorderSpaceMembers("space/one", {
    expectedVersion: 3,
    orderedSiteIds: ["site/three", "site/two"],
    beforeSiteId: null,
  }, controller.signal);

  for (const request of requests) assert.equal(request.init?.signal, controller.signal);

  assert.equal(requests[0]?.input, "/api/backend/spaces");
  assert.equal(requests[0]?.init?.method, "POST");
  assert.equal(new Headers(requests[0]?.init?.headers).get("content-type"), "application/json");
  assert.deepEqual(JSON.parse(String(requests[0]?.init?.body)), { name: "Research Notes" });

  assert.equal(requests[1]?.input, "/api/backend/spaces/space%2Fone");
  assert.equal(requests[1]?.init?.method, "PATCH");
  assert.deepEqual(JSON.parse(String(requests[1]?.init?.body)), {
    expected_version: 2,
    name: "New name",
  });

  assert.equal(requests[2]?.input, "/api/backend/spaces/space%2Fone/members");
  assert.deepEqual(JSON.parse(String(requests[2]?.init?.body)), {
    expected_version: 2,
    site_id: "site/one",
  });

  assert.equal(requests[3]?.input, "/api/backend/spaces/space%2Fone/members/order");
  assert.deepEqual(JSON.parse(String(requests[3]?.init?.body)), {
    expected_version: 3,
    ordered_site_ids: ["site/three", "site/two"],
    before_site_id: null,
  });
});

test("maps preview, versioned delete and encoded member removal routes", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  const controller = new AbortController();
  globalThis.fetch = async (input, init) => {
    requests.push({ input: String(input), init });
    let payload: unknown;
    if (String(input).endsWith("delete-preview")) {
      payload = { space, affected_site_count: 1 };
    } else if (String(input).includes("/members/")) {
      payload = {
        message: "网站已从 Space 移除",
        space_id: "space/one",
        site_id: "site/two",
        member_count: 0,
        version: 3,
      };
    } else {
      payload = {
        message: "Space 已删除",
        space_id: "space/one",
        unlinked_site_count: 1,
      };
    }
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  const preview = await previewSpaceDelete("space/one", controller.signal);
  const deleted = await deleteSpace("space/one", 2, controller.signal);
  const removed = await removeSpaceMember("space/one", "site/two", 2, controller.signal);

  assert.equal(requests[0]?.input, "/api/backend/spaces/space%2Fone/delete-preview");
  assert.equal(requests[1]?.input, "/api/backend/spaces/space%2Fone?expected_version=2");
  assert.equal(requests[1]?.init?.method, "DELETE");
  assert.equal(
    requests[2]?.input,
    "/api/backend/spaces/space%2Fone/members/site%2Ftwo?expected_version=2",
  );
  assert.equal(preview.affectedSiteCount, 1);
  assert.equal(deleted.unlinkedSiteCount, 1);
  assert.equal(removed.version, 3);
  for (const request of requests) assert.equal(request.init?.signal, controller.signal);
});

test("surfaces structured backend codes and rejects invalid input before fetching", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return new Response(JSON.stringify({
      detail: {
        code: "version_conflict",
        message: "Space 已被修改，请刷新后重试",
      },
    }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    });
  };

  await assert.rejects(
    updateSpace("space-1", { expectedVersion: 1, name: "New" }),
    (error: unknown) =>
      error instanceof SpaceApiError
      && error.status === 409
      && error.code === "version_conflict"
      && error.message === "Space 已被修改，请刷新后重试",
  );
  assert.equal(fetchCount, 1);

  await assert.rejects(deleteSpace("space-1", 0), /正整数/);
  await assert.rejects(addSpaceMember("space-1", { expectedVersion: 1, siteId: " " }), /非空字符串/);
  await assert.rejects(getSpace(" "), /资源 ID 不能为空/);
  assert.equal(fetchCount, 1);
});

test("keeps status fallbacks when a structured error omits its message", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: { code: "member_not_found" } }),
    { status: 404, headers: { "Content-Type": "application/json" } },
  );

  await assert.rejects(
    removeSpaceMember("space-1", "site-1", 2),
    (error: unknown) => {
      assert.ok(error instanceof SpaceApiError);
      assert.equal(error.status, 404);
      assert.equal(error.code, "member_not_found");
      assert.equal(error.message, "请求的 Space 或网站不存在");
      return true;
    },
  );
});
