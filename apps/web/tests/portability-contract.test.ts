import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizePortabilityPreview,
  normalizePortabilityResult,
  portabilityFileError,
  PORTABILITY_MAX_BYTES,
  PortabilityContractError,
} from "../lib/portability-contract.ts";

function preview() {
  return {
    id: "preview-1", status: "previewed", expires_at: "2026-10-24T08:00:00Z", payload_sha256: "a".repeat(64),
    counts: {
      categories: { create: 2, reuse: 1 }, tags: { create: 3, reuse: 4 },
      sites: { create: 6, merge: 2, skip_invalid: 1 }, spaces: { create: 1, merge: 1 },
      feeds: { create: 2, skip_existing: 3 }, conversations: { skipped: 4 },
    },
    warnings: [{ code: "invalid_url", message: "第 9 条收藏的网址无效，已跳过" }],
    samples: { sites_create: [{ name: "示例收藏", url: "https://example.com" }], categories_create: [{ name: "设计" }] },
  };
}

test("normalizes all backup preview counts, warnings, dates and per-category samples", () => {
  const normalized = normalizePortabilityPreview(preview());
  assert.equal(normalized.payloadSha256, "a".repeat(64));
  assert.equal(normalized.expiresAt, "2026-10-24T08:00:00Z");
  assert.deepEqual(normalized.counts.sites, { create: 6, merge: 2, reuse: 0, skipped: 1 });
  assert.equal(normalized.counts.feeds.skipped, 3);
  assert.equal(normalized.counts.categories.reuse, 1);
  assert.equal(normalized.counts.conversations.skipped, 4);
  assert.equal(normalized.warnings[0].code, "invalid_url");
  assert.deepEqual(normalized.samples.categories_create, [{ name: "设计", url: null }]);
});

test("accepts legacy preview responses without optional warnings or samples", () => {
  const old = { ...preview(), warnings: undefined, samples: undefined };
  assert.deepEqual(normalizePortabilityPreview(old).warnings, []);
  assert.deepEqual(normalizePortabilityPreview(old).samples, {});
});

test("caps sample display at twenty and keeps invalid URL samples as plain text", () => {
  const raw = { ...preview(), samples: { sites_skip_invalid: Array.from({ length: 25 }, (_, index) => ({ name: `收藏 ${index}`, url: "javascript:alert(1)" })) } };
  const normalized = normalizePortabilityPreview(raw);
  assert.equal(normalized.samples.sites_skip_invalid?.length, 20);
  assert.equal(normalized.samples.sites_skip_invalid?.[0].url, "javascript:alert(1)");
});

test("rejects malformed backup preview counters, status, checksum, dates and sample fields", () => {
  const invalid = [
    { ...preview(), status: "applied" }, { ...preview(), payload_sha256: "bad" },
    { ...preview(), expires_at: "not-a-date" }, { ...preview(), warnings: {} },
    { ...preview(), counts: { ...preview().counts, sites: { create: -1 } } },
    { ...preview(), counts: { ...preview().counts, tags: { create: "3" } } },
    { ...preview(), samples: { sites_create: [{ name: "sample", url: 1 }] } },
  ];
  for (const value of invalid) assert.throws(() => normalizePortabilityPreview(value), PortabilityContractError);
});

test("normalizes applied totals with absent optional kinds and rejects invalid totals", () => {
  const raw = { id: "preview-1", status: "applied", applied_at: "2026-10-23T09:00:00Z", created: { sites: 6 }, merged: { sites: 2 } };
  const result = normalizePortabilityResult(raw);
  assert.equal(result.appliedAt, raw.applied_at);
  assert.equal(result.created.sites, 6);
  assert.equal(result.created.tags, 0);
  assert.equal(result.merged.sites, 2);
  assert.throws(() => normalizePortabilityResult({ ...raw, merged: { sites: 1.5 } }), PortabilityContractError);
  assert.throws(() => normalizePortabilityResult({ ...raw, status: "previewed" }), PortabilityContractError);
});

test("validates JSON extension, empty files and the inclusive 64 MiB file limit", () => {
  assert.equal(portabilityFileError({ name: "backup.JSON", size: PORTABILITY_MAX_BYTES }), null);
  assert.match(portabilityFileError({ name: "backup.json", size: PORTABILITY_MAX_BYTES + 1 }) ?? "", /64 MiB/u);
  assert.match(portabilityFileError({ name: "backup.json", size: 0 }) ?? "", /为空/u);
  assert.match(portabilityFileError({ name: "backup.html", size: 100 }) ?? "", /\.json/u);
});
