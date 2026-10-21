import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";

import {
  buildLibrarySiteSearchParams,
  fetchLibraryOverview,
  fetchRediscover,
  LibraryApiError,
  listLibrarySiteSelection,
  listLibrarySites,
  recordSiteOpen,
  updateLibrarySite,
  updateSiteState,
} from "../lib/library-client.ts";
import {
  assertLibraryRediscoverQuery,
  assertLibrarySiteStateInput,
  assertLibrarySiteUpdateInput,
  LIBRARY_SITE_KINDS,
  LibraryContractError,
  libraryRepoMeta,
  normalizeLibraryOverview,
  normalizeLibraryRediscover,
  normalizeLibrarySite,
  normalizeLibrarySiteOpen,
  type LibrarySiteKind,
  type LibrarySiteState,
  type LibrarySiteStateInput,
} from "../lib/library-contract.ts";

const legacySite = {
  id: "site-1",
  name: "LangGraph",
  original_url: "https://github.com/langchain-ai/langgraph",
  identity_url: "https://github.com/langchain-ai/langgraph",
  summary: null,
  description: null,
  favicon_url: null,
  preview_url: null,
  category: { id: "category-1", name: "开发", is_default: true, icon: "Code" },
  tags: [{ id: "tag-1", name: "Agent" }],
  pinned: false,
  source: "agent",
  analysis_status: "complete",
  analysis_phase: null,
  version: 2,
  created_at: "2026-09-08T10:00:00",
  updated_at: "2026-09-09T10:00:00",
};

const collectionSite = {
  ...legacySite,
  kind: "repo",
  kind_source: "auto",
  open_count: 3,
  last_opened_at: "2026-10-20T08:00:00",
  seen_at: "2026-10-01T08:00:00",
  starred: true,
  starred_at: "2026-10-02T09:30:00",
  resurface_muted: false,
  meta: { host: "github.com", owner: "langchain-ai", repo: "langgraph" },
};

const overview = {
  total: 6,
  by_kind: { repo: 2, article: 1, video: 1, website: 2 },
  unopened: 3,
  starred: 2,
  added_7d: 5,
  opened_7d: 1,
};

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

type CapturedRequest = { url: string; init?: RequestInit };

function captureFetch(
  context: TestContext,
  respond: (request: CapturedRequest) => Response,
): CapturedRequest[] {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  const requests: CapturedRequest[] = [];
  globalThis.fetch = async (input, init) => {
    const request = { url: String(input), init };
    requests.push(request);
    return respond(request);
  };
  return requests;
}

test("normalizes resource kind and usage fields on a site", () => {
  const site = normalizeLibrarySite(collectionSite);
  assert.equal(site.kind, "repo");
  assert.equal(site.kindSource, "auto");
  assert.equal(site.openCount, 3);
  assert.equal(site.lastOpenedAt, "2026-10-20T08:00:00");
  assert.equal(site.seenAt, "2026-10-01T08:00:00");
  assert.equal(site.starred, true);
  assert.equal(site.starredAt, "2026-10-02T09:30:00");
  assert.equal(site.resurfaceMuted, false);
  assert.deepEqual(site.meta, { host: "github.com", owner: "langchain-ai", repo: "langgraph" });
  // A frozen copy: UI code can never mutate the response object through it.
  assert.ok(Object.isFrozen(site.meta));
  assert.notEqual(site.meta, collectionSite.meta);
  assert.deepEqual(libraryRepoMeta(site.meta), {
    host: "github.com",
    owner: "langchain-ai",
    repo: "langgraph",
  });
});

test("an API response without the collection fields reads as a neutral website", () => {
  const site = normalizeLibrarySite(legacySite);
  assert.deepEqual(
    {
      kind: site.kind,
      kindSource: site.kindSource,
      openCount: site.openCount,
      lastOpenedAt: site.lastOpenedAt,
      seenAt: site.seenAt,
      starred: site.starred,
      starredAt: site.starredAt,
      resurfaceMuted: site.resurfaceMuted,
      meta: site.meta,
    },
    {
      kind: "website",
      kindSource: "auto",
      openCount: 0,
      lastOpenedAt: null,
      seenAt: null,
      starred: false,
      starredAt: null,
      resurfaceMuted: false,
      meta: {},
    },
  );
  assert.equal(libraryRepoMeta(site.meta), null);
  // Only one of the pair present: the other is derived, not contradicted.
  const derived = normalizeLibrarySite({ ...legacySite, starred_at: "2026-10-02T09:30:00" });
  assert.equal(derived.starred, true);
});

test("present but malformed collection fields are contract errors", () => {
  const invalid: Array<Record<string, unknown>> = [
    { kind: "podcast" },
    { kind: null },
    { kind_source: "robot" },
    { open_count: -1 },
    { open_count: 1.5 },
    { open_count: "3" },
    { last_opened_at: "not a date" },
    { seen_at: 12 },
    { starred: "true" },
    { starred: true, starred_at: null },
    { starred: false, starred_at: "2026-10-02T09:30:00" },
    { resurface_muted: 0 },
    { meta: [] },
    { meta: "github.com" },
    { meta: null },
  ];
  for (const fields of invalid) {
    assert.throws(
      () => normalizeLibrarySite({ ...collectionSite, ...fields }),
      LibraryContractError,
      JSON.stringify(fields),
    );
  }
});

test("repository metadata requires host, owner and repo strings", () => {
  assert.deepEqual(
    libraryRepoMeta({ host: "huggingface.co", owner: "openai", repo: "gsm8k", repo_type: "dataset" }),
    { host: "huggingface.co", owner: "openai", repo: "gsm8k", repoType: "dataset" },
  );
  assert.equal(libraryRepoMeta({ host: "github.com", owner: "a" }), null);
  assert.equal(libraryRepoMeta({ host: "github.com", owner: "a", repo: 3 }), null);
  assert.equal(libraryRepoMeta({ host: " ", owner: "a", repo: "b" }), null);
});

test("normalizes open results, rediscover picks and the overview", () => {
  assert.deepEqual(
    normalizeLibrarySiteOpen({
      site_id: "site-1",
      open_count: 1,
      last_opened_at: "2026-10-21T08:00:00",
      seen_at: "2026-10-21T08:00:00",
    }),
    {
      siteId: "site-1",
      openCount: 1,
      lastOpenedAt: "2026-10-21T08:00:00",
      seenAt: "2026-10-21T08:00:00",
    },
  );
  for (const payload of [
    { site_id: "site-1", open_count: 0, last_opened_at: "2026-10-21", seen_at: "2026-10-21" },
    { site_id: "site-1", open_count: 1, last_opened_at: "2026-10-21", seen_at: null },
    { open_count: 1, last_opened_at: "2026-10-21", seen_at: "2026-10-21" },
  ]) {
    assert.throws(() => normalizeLibrarySiteOpen(payload), LibraryContractError);
  }

  const rediscover = normalizeLibraryRediscover({
    generated_for: "2026-10-21",
    items: [
      {
        site: collectionSite,
        saved_days: 43,
        reason: "43 天前收藏，一次都没打开过；和你最近常收藏的「Agent」相关",
      },
    ],
  });
  assert.equal(rediscover.generatedFor, "2026-10-21");
  assert.equal(rediscover.items[0]?.site.kind, "repo");
  assert.equal(rediscover.items[0]?.savedDays, 43);
  assert.match(rediscover.items[0]?.reason ?? "", /一次都没打开过/u);
  assert.deepEqual(normalizeLibraryRediscover({ generated_for: "2026-10-21", items: [] }).items, []);

  assert.deepEqual(normalizeLibraryOverview(overview), {
    total: 6,
    byKind: { repo: 2, article: 1, video: 1, website: 2 },
    unopened: 3,
    starred: 2,
    added7d: 5,
    opened7d: 1,
  });
});

test("rejects inconsistent rediscover and overview payloads", () => {
  const item = { site: collectionSite, saved_days: 4, reason: "4 天前收藏，一次都没打开过" };
  const rediscover: unknown[] = [
    { items: [] },
    { generated_for: "2026/10/21", items: [] },
    { generated_for: "2026-02-30", items: [] },
    { generated_for: "2026-10-21T00:00:00Z", items: [] },
    { generated_for: "2026-10-21", items: {} },
    { generated_for: "2026-10-21", items: [item, item] },
    { generated_for: "2026-10-21", items: [{ ...item, saved_days: -1 }] },
    { generated_for: "2026-10-21", items: [{ ...item, reason: " " }] },
    { generated_for: "2026-10-21", items: [{ ...item, site: { ...collectionSite, kind: "x" } }] },
    {
      generated_for: "2026-10-21",
      items: Array.from({ length: 13 }, (_, index) => ({
        ...item,
        site: { ...collectionSite, id: `site-${index}` },
      })),
    },
  ];
  for (const payload of rediscover) {
    assert.throws(() => normalizeLibraryRediscover(payload), LibraryContractError);
  }

  const overviews: unknown[] = [
    { ...overview, total: 7 },
    { ...overview, by_kind: { repo: 2, article: 1, video: 1 } },
    { ...overview, unopened: 7, total: 6 },
    { ...overview, opened_7d: -1 },
    { ...overview, by_kind: null },
  ];
  for (const payload of overviews) {
    assert.throws(() => normalizeLibraryOverview(payload), LibraryContractError);
  }
});

test("validates state updates, rediscover queries and kind edits before sending", () => {
  assert.deepEqual(assertLibrarySiteStateInput({ starred: true, snoozeDays: 0 }), {
    starred: true,
    snoozeDays: 0,
  });
  assert.deepEqual(assertLibrarySiteStateInput({ seen: false, snoozeDays: 90 }), {
    seen: false,
    snoozeDays: 90,
  });
  const invalidStates: LibrarySiteStateInput[] = [
    {},
    { snoozeDays: 91 },
    { snoozeDays: -1 },
    { snoozeDays: 1.5 },
    { snoozeDays: Number.NaN },
    { starred: "true" as unknown as boolean },
    { resurfaceMuted: 1 as unknown as boolean },
  ];
  for (const input of invalidStates) {
    assert.throws(() => assertLibrarySiteStateInput(input), LibraryContractError);
  }

  assert.deepEqual(assertLibraryRediscoverQuery({}), { limit: 4, shuffle: 0 });
  assert.deepEqual(assertLibraryRediscoverQuery({ limit: 12, shuffle: 3 }), {
    limit: 12,
    shuffle: 3,
  });
  for (const query of [{ limit: 0 }, { limit: 13 }, { limit: 2.5 }, { shuffle: -1 }]) {
    assert.throws(() => assertLibraryRediscoverQuery(query), LibraryContractError);
  }

  assert.deepEqual(assertLibrarySiteUpdateInput({ expectedVersion: 2, kind: "article" }), {
    expectedVersion: 2,
    kind: "article",
  });
  assert.throws(
    () => assertLibrarySiteUpdateInput({
      expectedVersion: 2,
      kind: "podcast" as LibrarySiteKind,
    }),
    LibraryContractError,
  );
  assert.deepEqual([...LIBRARY_SITE_KINDS], ["repo", "article", "video", "website"]);
});

test("list queries carry kind, state and the opened sort", async (context) => {
  const params = buildLibrarySiteSearchParams({
    kind: "repo",
    state: "unopened",
    sort: "opened",
    direction: "desc",
  });
  assert.equal(params.get("kind"), "repo");
  assert.equal(params.get("state"), "unopened");
  assert.equal(params.get("sort"), "opened");
  assert.equal(buildLibrarySiteSearchParams({}).has("kind"), false);
  assert.equal(buildLibrarySiteSearchParams({}).has("state"), false);
  assert.throws(
    () => buildLibrarySiteSearchParams({ kind: "podcast" as LibrarySiteKind }),
    LibraryContractError,
  );
  assert.throws(
    () => buildLibrarySiteSearchParams({ state: "archived" as LibrarySiteState }),
    LibraryContractError,
  );

  const requests = captureFetch(context, ({ url }) =>
    url.includes("/selection")
      ? json({
        items: [{
          id: "site-1",
          name: "LangGraph",
          original_url: collectionSite.original_url,
          favicon_url: null,
          version: 2,
        }],
      })
      : json({
        items: [collectionSite],
        next_cursor: null,
        aggregate: { matched_count: 1, pinned_count: 0 },
      }));
  const page = await listLibrarySites({ kind: "repo", state: "starred", sort: "opened" });
  assert.equal(page.items[0]?.starred, true);
  await listLibrarySiteSelection({ kind: "video", state: "opened", sort: "opened" });
  const listUrl = new URL(requests[0]?.url ?? "", "http://webhub.test");
  assert.equal(listUrl.pathname, "/api/backend/library/sites");
  assert.equal(listUrl.searchParams.get("kind"), "repo");
  assert.equal(listUrl.searchParams.get("state"), "starred");
  assert.equal(listUrl.searchParams.get("sort"), "opened");
  const selectionUrl = new URL(requests[1]?.url ?? "", "http://webhub.test");
  assert.equal(selectionUrl.pathname, "/api/backend/library/sites/selection");
  assert.equal(selectionUrl.searchParams.get("kind"), "video");
  assert.equal(selectionUrl.searchParams.get("state"), "opened");
  assert.equal(selectionUrl.searchParams.has("sort"), false);
});

test("recordSiteOpen posts once with keepalive and checks the echoed id", async (context) => {
  let responseSiteId = "site/one";
  const requests = captureFetch(context, () =>
    json({
      site_id: responseSiteId,
      open_count: 2,
      last_opened_at: "2026-10-21T08:00:00",
      seen_at: "2026-10-01T08:00:00",
    }));

  const result = await recordSiteOpen("site/one");
  assert.deepEqual(result, {
    siteId: "site/one",
    openCount: 2,
    lastOpenedAt: "2026-10-21T08:00:00",
    seenAt: "2026-10-01T08:00:00",
  });
  assert.equal(requests[0]?.url, "/api/backend/library/sites/site%2Fone/open");
  assert.equal(requests[0]?.init?.method, "POST");
  assert.equal(requests[0]?.init?.keepalive, true);
  assert.equal(requests[0]?.init?.credentials, "include");
  assert.equal(requests[0]?.init?.body, undefined);

  responseSiteId = "another-site";
  await assert.rejects(recordSiteOpen("site/one"), LibraryContractError);
  await assert.rejects(recordSiteOpen("   "), TypeError);
  assert.equal(requests.length, 2);
});

test("recordSiteOpen and updateSiteState surface account-scoped 404s", async (context) => {
  captureFetch(context, () =>
    json({ detail: { code: "not_found", message: "网站不存在" } }, 404));
  await assert.rejects(recordSiteOpen("site-9"), (error: unknown) => {
    assert.ok(error instanceof LibraryApiError);
    assert.equal(error.status, 404);
    assert.equal(error.code, "not_found");
    assert.equal(error.message, "网站不存在");
    return true;
  });
  await assert.rejects(updateSiteState("site-9", { starred: true }), LibraryApiError);
});

test("updateSiteState sends a snake_case PUT and rejects bad input before fetching", async (context) => {
  const requests = captureFetch(context, () => json({ ...collectionSite, resurface_muted: true }));

  const site = await updateSiteState("site-1", {
    starred: false,
    seen: true,
    snoozeDays: 7,
    resurfaceMuted: true,
  });
  assert.equal(site.resurfaceMuted, true);
  assert.equal(requests[0]?.url, "/api/backend/library/sites/site-1/state");
  assert.equal(requests[0]?.init?.method, "PUT");
  assert.deepEqual(JSON.parse(String(requests[0]?.init?.body)), {
    starred: false,
    seen: true,
    snooze_days: 7,
    resurface_muted: true,
  });
  assert.equal(
    (requests[0]?.init?.headers as Record<string, string>)["Content-Type"],
    "application/json",
  );

  await updateSiteState("site-1", { snoozeDays: 0 });
  assert.deepEqual(JSON.parse(String(requests[1]?.init?.body)), { snooze_days: 0 });

  await assert.rejects(updateSiteState("site-1", {}), LibraryContractError);
  await assert.rejects(updateSiteState("site-1", { snoozeDays: 120 }), LibraryContractError);
  assert.equal(requests.length, 2);
});

test("fetchRediscover and fetchLibraryOverview read the new endpoints", async (context) => {
  const requests = captureFetch(context, ({ url }) =>
    url.includes("/discover/")
      ? json({
        generated_for: "2026-10-21",
        items: [{ site: collectionSite, saved_days: 43, reason: "43 天前收藏，一次都没打开过" }],
      })
      : json(overview));

  const picks = await fetchRediscover();
  assert.equal(picks.items.length, 1);
  assert.equal(requests[0]?.url, "/api/backend/discover/rediscover?limit=4&shuffle=0");
  assert.equal(requests[0]?.init?.credentials, "include");
  assert.equal(requests[0]?.init?.cache, "no-store");
  await fetchRediscover({ limit: 12, shuffle: 5 });
  assert.equal(requests[1]?.url, "/api/backend/discover/rediscover?limit=12&shuffle=5");
  await assert.rejects(fetchRediscover({ limit: 13 }), LibraryContractError);
  await assert.rejects(fetchRediscover({ shuffle: -1 }), LibraryContractError);

  const counts = await fetchLibraryOverview();
  assert.equal(requests[2]?.url, "/api/backend/library/overview");
  assert.equal(counts.byKind.repo, 2);
  assert.equal(counts.opened7d, 1);
  assert.equal(requests.length, 3);
});

test("site PATCH can set a manual kind", async (context) => {
  const requests = captureFetch(context, () => json({ ...collectionSite, kind: "article", kind_source: "manual" }));
  const updated = await updateLibrarySite("site-1", { expectedVersion: 2, kind: "article" });
  assert.equal(updated.kindSource, "manual");
  assert.deepEqual(JSON.parse(String(requests[0]?.init?.body)), {
    expected_version: 2,
    kind: "article",
  });
});
