import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { MAX_PROXIED_IMAGE_URL_LENGTH, MEDIA_PROXY_PATH, proxiedImageUrl } from "../lib/media-proxy.ts";

const ROOT = new URL("../", import.meta.url);
const read = (path: string) => readFile(new URL(path, ROOT), "utf8");

function target(proxied: string | null): string | null {
  if (proxied === null) return null;
  assert.ok(proxied.startsWith(`${MEDIA_PROXY_PATH}?url=`));
  return new URLSearchParams(proxied.slice(MEDIA_PROXY_PATH.length + 1)).get("url");
}

test("external http(s) images map to the same-origin proxy with an encoded url", () => {
  const proxied = proxiedImageUrl("https://cdn.example.com/a b/icon.png?x=1&y=2");
  assert.equal(proxied, `${MEDIA_PROXY_PATH}?url=${encodeURIComponent("https://cdn.example.com/a%20b/icon.png?x=1&y=2")}`);
  assert.equal(target(proxied), "https://cdn.example.com/a%20b/icon.png?x=1&y=2");
  assert.equal(target(proxiedImageUrl("  http://example.com/favicon.ico  ")), "http://example.com/favicon.ico");
});

test("fragments are dropped and equivalent spellings share one proxy url", () => {
  assert.equal(proxiedImageUrl("https://Example.COM/a.png#top"), proxiedImageUrl("https://example.com/a.png"));
  assert.equal(target(proxiedImageUrl("https://example.com")), "https://example.com/");
});

test("values that cannot be proxied return null so callers fall back locally", () => {
  for (const value of [
    null,
    undefined,
    "",
    "   ",
    "/icons/local.png",
    "//cdn.example.com/a.png",
    "javascript:alert(1)",
    "data:image/png;base64,AAAA",
    "blob:https://example.com/123",
    "ftp://example.com/a.png",
    "https://user:pass@example.com/a.png",
    "not a url",
    `https://example.com/${"a".repeat(MAX_PROXIED_IMAGE_URL_LENGTH)}`,
  ]) {
    assert.equal(proxiedImageUrl(value), null, String(value));
  }
});

test("mapping is idempotent for an already proxied url", () => {
  const proxied = proxiedImageUrl("https://example.com/a.png");
  assert.equal(proxiedImageUrl(proxied), proxied);
});

test("site favicon and cover preview load through the proxy, never the raw url", async () => {
  const [favicon, cover] = await Promise.all([
    read("components/site-favicon.tsx"),
    read("components/discover/resource-cover.tsx"),
  ]);
  assert.match(favicon, /const src = proxiedImageUrl\(url\);/u);
  assert.match(favicon, /src=\{src\}/u);
  assert.doesNotMatch(favicon, /src=\{url\}/u);
  assert.match(cover, /proxiedImageUrl\(model\.src\)/u);
  assert.match(cover, /src=\{imageSrc\}/u);
  assert.doesNotMatch(cover, /src=\{model\.src\}/u);
});
