import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const ROOT = new URL("../", import.meta.url);
const read = (path) => readFile(new URL(path, ROOT), "utf8");

test("the feeds stylesheet uses design tokens only and keeps radii at or below 8px", async () => {
  const css = await read("app/styles/feeds.css");
  assert.doesNotMatch(css, /#[0-9a-fA-F]{3,8}\b/u);
  assert.doesNotMatch(css, /\b(?:rgb|rgba|hsl|hsla|oklch)\(/u);
  for (const match of css.matchAll(/border-radius:\s*([^;]+);/gu)) {
    const value = match[1].trim();
    assert.match(value, /^(?:var\(--radius[a-z-]*\)|[0-8]px|50%|999px)$/u, value);
    assert.notEqual(value, "999px");
    assert.notEqual(value, "50%");
  }
  assert.match(await read("app/globals.css"), /@import "\.\/styles\/feeds\.css";/u);
});

test("the feeds workspace exposes the three views, real buttons and every state", async () => {
  const source = await read("components/feeds/feeds-workspace.tsx");
  for (const label of ["今日精选", "全部", "订阅管理"]) assert.ok(source.includes(label), label);
  assert.match(source, /from "lucide-react"/u);
  assert.doesNotMatch(source, /<div[^>]*onClick/u);
  assert.match(source, /收进收藏库/u);
  assert.match(source, /role="alert"/u);
  assert.match(source, /provider-empty/u);
  assert.match(source, /provider-loading/u);
  assert.match(source, /rel="noopener noreferrer"/u);
  assert.match(source, /撤销/u);
  assert.match(await read("app/(workspace)/feeds/page.tsx"), /FeedsWorkspace/u);
});
