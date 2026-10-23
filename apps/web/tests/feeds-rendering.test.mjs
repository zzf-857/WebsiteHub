import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const ROOT = new URL("../", import.meta.url);
const read = (path) => readFile(new URL(path, ROOT), "utf8");

test("the feeds stylesheet uses design tokens only and keeps radii at or below 8px", async () => {
  const css = await read("app/styles/feeds.css");
  assert.doesNotMatch(css, /#[0-9a-fA-F]{3,8}\b/u);
  // 来源色块按 --site-hue 派生，只允许引用封面令牌的 oklch(var(--cover-…))
  assert.doesNotMatch(css, /\b(?:rgb|rgba|hsl|hsla)\(/u);
  assert.doesNotMatch(css, /\boklch\((?!var\(--cover-)/u);
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

test("feed rows: source mark, icon actions with visible tooltips, link titles and the shared container width", async () => {
  const source = await read("components/feeds/feeds-workspace.tsx");
  const css = await read("app/styles/feeds.css");
  // 来源标识：站点图标 / 字母块 / 播客耳机块；没有图片字段，不画缩略图
  assert.match(source, /feedMark\(/u);
  assert.match(source, /Headphones size=\{18\}/u);
  // 操作是图标按钮，文字名放在 aria-label 与可见提示里，不再重复“收进收藏库”文字按钮
  assert.match(source, /data-tooltip=/u);
  assert.match(source, /aria-label=\{item\.url \? `收进收藏库：/u);
  assert.doesNotMatch(source, />\s*收进收藏库\s*</u);
  assert.match(css, /\.feed-action\[data-tooltip\]:focus-visible::after/u);
  assert.match(css, /@media \(hover: hover\)[\s\S]*\.feed-card:focus-within \.feed-card-actions/u);
  assert.match(css, /@media \(hover: none\)/u);
  // 标题是新标签打开的链接，悬停 / 聚焦出现右上箭头
  assert.match(source, /ArrowUpRight/u);
  assert.match(css, /a\.feed-card-title:focus-visible \.feed-card-title-arrow/u);
  // 与收藏库 / 发现页同一档容器宽度；精选说明只在今日精选视图里
  assert.match(css, /\.site-main\.feeds-workspace \{\s*width: min\(calc\(100% - 48px\), var\(--discover-max\)\)/u);
  assert.match(source, /feeds-picks-note/u);
  assert.doesNotMatch(source.slice(source.indexOf("provider-page-lead"), source.indexOf("feeds-tabs")), /同一来源最多/u);
});
