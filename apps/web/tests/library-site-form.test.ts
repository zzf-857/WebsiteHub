import assert from "node:assert/strict";
import test from "node:test";

import type { LibrarySite } from "../lib/library-contract.ts";
import {
  buildLibrarySiteUpdate,
  type LibrarySiteFormValues,
} from "../lib/library-site-form.ts";

const site: LibrarySite = {
  id: "site-1",
  name: "Example",
  originalUrl: "https://old.example/",
  identityUrl: "https://old.example",
  summary: "A concise but complete saved site summary",
  description: "Saved description",
  faviconUrl: "https://old.example/favicon.ico",
  previewUrl: "https://old.example/preview.png",
  category: { id: "category-1", name: "默认", isDefault: true, icon: "Folder" },
  tags: [{ id: "tag-1", name: "Docs" }],
  pinned: false,
  source: "manual",
  analysisStatus: "complete",
  analysisPhase: null,
  version: 4,
  createdAt: "2026-07-27T00:00:00Z",
  updatedAt: "2026-07-27T00:00:00Z",
};

const values: LibrarySiteFormValues = {
  name: site.name,
  url: site.originalUrl,
  summary: site.summary ?? "",
  description: site.description ?? "",
  faviconUrl: site.faviconUrl ?? "",
  categoryId: site.category.id,
  tagIds: site.tags.map((tag) => tag.id),
  pinned: site.pinned,
};

test("site edit PATCH omits untouched fields from a stale form snapshot", () => {
  assert.equal(buildLibrarySiteUpdate(site, values), null);
  assert.deepEqual(
    buildLibrarySiteUpdate(site, { ...values, url: "https://new.example/" }),
    { expectedVersion: 4, url: "https://new.example/" },
  );
});

test("site edit PATCH preserves explicit favicon changes and clears", () => {
  assert.deepEqual(
    buildLibrarySiteUpdate(site, {
      ...values,
      url: "https://new.example/",
      faviconUrl: "https://new.example/icon.png",
    }),
    {
      expectedVersion: 4,
      url: "https://new.example/",
      faviconUrl: "https://new.example/icon.png",
    },
  );
  assert.deepEqual(buildLibrarySiteUpdate(site, { ...values, faviconUrl: "" }), {
    expectedVersion: 4,
    faviconUrl: null,
  });
});

test("site edit PATCH treats summary and detailed description independently", () => {
  assert.deepEqual(buildLibrarySiteUpdate(site, {
    ...values,
    summary: "A concise but complete replacement summary",
  }), {
    expectedVersion: 4,
    summary: "A concise but complete replacement summary",
  });
  assert.deepEqual(buildLibrarySiteUpdate(site, { ...values, description: "" }), {
    expectedVersion: 4,
    description: null,
  });
});
