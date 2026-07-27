import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const PROVIDERS_PAGE = new URL(
  "../app/(workspace)/settings/providers/page.tsx",
  import.meta.url,
);
const SEARCH_INDEX_PAGE = new URL(
  "../app/(workspace)/settings/search-index/page.tsx",
  import.meta.url,
);
const SITE_HEADER = new URL("../components/site-header.tsx", import.meta.url);
const SEMANTIC_PANEL = new URL(
  "../components/settings/semantic-index-panel.tsx",
  import.meta.url,
);
const PROVIDERS_CSS = new URL("../app/styles/providers.css", import.meta.url);

test("语义索引拥有独立设置页，不再挂在服务商页面的主内容区外", async () => {
  const [providersPage, searchIndexPage, siteHeader] = await Promise.all([
    readFile(PROVIDERS_PAGE, "utf8"),
    readFile(SEARCH_INDEX_PAGE, "utf8"),
    readFile(SITE_HEADER, "utf8"),
  ]);

  assert.doesNotMatch(providersPage, /SemanticIndexPanel/);
  assert.match(searchIndexPage, /<main className="site-main">/);
  assert.match(searchIndexPage, /<SemanticIndexPanel \/>/);
  assert.match(siteHeader, /href="\/settings\/search-index"/);
  assert.match(siteHeader, /<span>语义索引<\/span>/);
});

test("全部重建使用后端专用费用并保留文字确认按钮样式", async () => {
  const [panel, providersCss] = await Promise.all([
    readFile(SEMANTIC_PANEL, "utf8"),
    readFile(PROVIDERS_CSS, "utf8"),
  ]);

  assert.match(panel, /status\.rebuildEstimatedRequests/);
  assert.match(panel, /status\.rebuildPassEstimatedRequests/);
  assert.match(panel, /status\.passLimit/);
  assert.match(panel, /run\.reason === "provider_unavailable"/);
  assert.match(panel, /run\.reason === "already_running"/);
  assert.match(panel, /<h2[^>]*>[\s\S]*?<Database/);
  assert.doesNotMatch(providersCss, /\.provider-notice button\s*\{/);
  assert.match(providersCss, /\.provider-notice > button\s*\{/);
});
