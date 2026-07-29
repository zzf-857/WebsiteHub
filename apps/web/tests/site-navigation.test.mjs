import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  ANALYSIS_REFRESH_DELAYS_MS,
  analysisRefreshDelay,
} from "../lib/analysis-refresh.ts";

const HOME_COMPONENTS = [
  new URL("../components/home/category-sections.tsx", import.meta.url),
  new URL("../components/home/pinned-sites.tsx", import.meta.url),
  new URL("../components/home/recent-sites.tsx", import.meta.url),
];
const AGENT_CARDS = new URL("../components/agent/agent-result-cards.tsx", import.meta.url);
const LIBRARY_CARDS = new URL(
  "../components/library/library-workspace-parts.tsx",
  import.meta.url,
);
const LIBRARY_WORKSPACE_HOOK = new URL(
  "../components/library/use-library-workspace.tsx",
  import.meta.url,
);
const SITE_DETAIL = new URL(
  "../components/library/detail/site-detail-page.tsx",
  import.meta.url,
);

test("saved-site cards lead to the internal detail page", async () => {
  const [homeSources, agentCards, libraryCards] = await Promise.all([
    Promise.all(HOME_COMPONENTS.map((file) => readFile(file, "utf8"))),
    readFile(AGENT_CARDS, "utf8"),
    readFile(LIBRARY_CARDS, "utf8"),
  ]);

  for (const source of homeSources) {
    assert.match(source, /href={`\/library\/\$\{encodeURIComponent\(site\.id\)\}`}/);
    assert.doesNotMatch(source, /href={site\.originalUrl}/);
  }
  assert.match(agentCards, /href={`\/library\/\$\{encodeURIComponent\(link\.siteId\)\}`}/);
  assert.match(libraryCards, /className="library-site-card-link"/);
  assert.match(libraryCards, /href={`\/library\/\$\{encodeURIComponent\(site\.id\)\}`}/);
  assert.doesNotMatch(libraryCards, /href=\{site\.originalUrl\}/);
  assert.doesNotMatch(libraryCards, /target="_blank"/);
});

test("metadata backfill refreshes the visible library for a bounded period", async () => {
  const source = await readFile(LIBRARY_WORKSPACE_HOOK, "utf8");
  assert.deepEqual(
    [...ANALYSIS_REFRESH_DELAYS_MS],
    [1_000, 2_000, 3_000, 5_000, 8_000, 12_000, ...Array(18).fill(15_000)],
  );
  assert.equal(analysisRefreshDelay(ANALYSIS_REFRESH_DELAYS_MS.length - 1), 15_000);
  assert.equal(analysisRefreshDelay(ANALYSIS_REFRESH_DELAYS_MS.length), null);
  assert.match(source, /hasRefreshableSiteAnalysis\(\[\.\.\.pinnedPage\.items, \.\.\.regularPage\.items\]\)/);
  assert.match(source, /useBoundedAnalysisRefresh\(\{[\s\S]*?scope: selectionScope,[\s\S]*?enabled: analysisRefreshEnabled,[\s\S]*?refresh: refreshSitesForAnalysis/);
  assert.match(source, /const result = await startMetadataBackfill\(\)[\s\S]*?refreshSites\(\)/);
});

test("site detail renders a compact, dismissible preview only when previewUrl exists", async () => {
  const source = await readFile(SITE_DETAIL, "utf8");
  assert.match(source, /previewUrl && previewUrl !== failedPreviewUrl/);
  assert.match(source, /previewAvailable && previewUrl/);
  assert.match(source, /className="sd-preview-row"/);
  assert.match(source, /className="sd-preview-trigger"/);
  assert.match(source, /className="sd-preview-overlay"/);
  assert.match(source, /aria-modal="true"/);
  assert.match(source, /event\.key === "Escape"/);
  assert.match(source, /event\.target === event\.currentTarget/);
  assert.match(source, /handlePreviewError/);
  assert.match(source, /referrerPolicy="no-referrer"/);
  assert.match(source, /className="sd-btn sd-btn-primary"/);
  assert.equal(
    source.match(/href=\{site\.originalUrl\}/g)?.length,
    1,
    "详情页只有明确的访问官网按钮可以外跳",
  );
});
