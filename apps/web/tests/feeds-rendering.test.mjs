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
  assert.match(await read("components/feeds/feed-item-row.tsx"), /rel="noopener noreferrer"/u);
  assert.match(source, /撤销/u);
  assert.match(await read("app/(workspace)/feeds/page.tsx"), /FeedsWorkspace/u);
});

test("feed rows: source mark, icon actions with visible tooltips, link titles and the shared container width", async () => {
  const workspace = await read("components/feeds/feeds-workspace.tsx");
  const source = await read("components/feeds/feed-item-row.tsx");
  const css = await read("app/styles/feeds.css");
  // 来源标识：频道封面 / 站点图标 / 字母块 / 播客耳机块。
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
  assert.match(workspace, /feeds-picks-note/u);
  assert.doesNotMatch(workspace.slice(workspace.indexOf("provider-page-lead"), workspace.indexOf("feeds-tabs")), /同一来源最多/u);
});


test("feed thumbnails use the media proxy and remove the image slot when loading fails", async () => {
  const source = await read("components/feeds/feed-item-row.tsx");
  assert.match(source, /const imageSrc = proxiedImageUrl\(item\.imageUrl\)/u);
  assert.match(source, /const thumbnail = imageSrc && imageSrc !== failedThumbnail \? imageSrc : null/u);
  assert.match(source, /data-has-image=\{thumbnail \? "true" : undefined\}/u);
  assert.match(source, /\{thumbnail && \([\s\S]*<img[\s\S]*className="feeds-item-thumbnail"/u);
  const thumbnail = source.match(/<img\s+className="feeds-item-thumbnail"[\s\S]*?\/>/u)?.[0];
  assert.ok(thumbnail, "the thumbnail element is present");
  assert.match(thumbnail, /src=\{thumbnail\}/u);
  assert.match(thumbnail, /alt=""/u);
  assert.match(thumbnail, /loading="lazy"/u);
  assert.match(thumbnail, /referrerPolicy="no-referrer"/u);
  assert.match(thumbnail, /onError=\{\(\) => setFailedThumbnail\(thumbnail\)\}/u);
  assert.doesNotMatch(source, /src=\{(?:item\.imageUrl|mark\.faviconUrl|mark\.imageUrl)\}/u);
});

test("feed thumbnails keep desktop and mobile dimensions without reserving an absent image slot", async () => {
  const css = await read("app/styles/feeds.css");
  assert.match(css, /\.feed-card\[data-has-image="true"\][\s\S]*grid-template-columns: 36px minmax\(0, 1fr\) 96px auto/u);
  assert.match(css, /\.feeds-item-thumbnail \{[\s\S]*width: 96px;[\s\S]*height: 64px;[\s\S]*object-fit: cover;[\s\S]*border: 1px solid var\(--line\);[\s\S]*border-radius: 6px;/u);
  assert.match(css, /@media \(width < 600px\)[\s\S]*\.feeds-item-thumbnail \{[\s\S]*width: 72px;[\s\S]*height: 48px;/u);
});

test("feed item rows live in their own component and keep the workspace below its size limit", async () => {
  const workspace = await read("components/feeds/feeds-workspace.tsx");
  const row = await read("components/feeds/feed-item-row.tsx");
  assert.match(workspace, /from "@\/components\/feeds\/feed-item-row"/u);
  assert.match(workspace, /<FeedItemRow/u);
  assert.doesNotMatch(workspace, /function FeedItemCard|function FeedSourceMark/u);
  assert.match(row, /export function FeedItemRow/u);
  assert.ok(workspace.split("\n").length <= 700);
  assert.ok(row.split("\n").length <= 700);
});
