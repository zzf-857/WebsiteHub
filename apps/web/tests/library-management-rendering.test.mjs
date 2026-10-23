import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

// Q46 的接线检查：详情页、收藏库管理模式与全站快捷键的结构约定。
// 交互本身由 keyboard-shortcuts / site-detail-edit 的纯逻辑测试覆盖，这里只防止接线被拆掉。

const read = (path) => readFile(new URL(`../${path}`, import.meta.url), "utf8");

test("the workspace mounts one global shortcut dispatcher and the header no longer listens itself", async () => {
  const [layout, header, shortcuts] = await Promise.all([
    read("app/(workspace)/layout.tsx"),
    read("components/site-header.tsx"),
    read("components/keyboard/global-shortcuts.tsx"),
  ]);
  assert.match(layout, /<GlobalShortcuts \/>/u);
  assert.doesNotMatch(header, /addEventListener\("keydown", handleKeydown\)/u);
  assert.match(header, /openShortcutHelp\(\)/u, "账号菜单里可以打开快捷键说明");
  assert.equal(shortcuts.match(/addEventListener\("keydown"/gu)?.length, 1);
  assert.match(shortcuts, /"go-discover": "\/"/u);
  assert.match(shortcuts, /"go-library": "\/library"/u);
  assert.match(shortcuts, /"go-spaces": "\/spaces"/u);
  assert.match(shortcuts, /dialog\[open\], \[aria-modal='true'\], \[data-shortcut-block\]/u);
  assert.match(shortcuts, /\[data-shortcut-default\]/u);
});

test("gallery cards, library cards and the detail page are shortcut targets", async () => {
  const [card, masonry, libraryParts, detail] = await Promise.all([
    read("components/discover/resource-card.tsx"),
    read("components/discover/gallery-masonry.tsx"),
    read("components/library/library-workspace-parts.tsx"),
    read("components/library/detail/site-detail-page.tsx"),
  ]);
  for (const source of [card, libraryParts]) {
    assert.match(source, /data-shortcut-item/u);
    assert.match(source, /data-shortcut-focus/u);
    assert.match(source, /useShortcutTarget/u);
  }
  assert.match(card, /data-shortcut-order=\{shortcutOrder\}/u, "瀑布流按数据顺序移动焦点");
  assert.match(masonry, /shortcutOrder=\{orderById\.get\(id\) \?\? 0\}/u);
  assert.match(detail, /ref=\{shortcutRef\} data-shortcut-default/u);
});

test("the detail page keeps destructive actions behind the more menu and a confirmation", async () => {
  const detail = await read("components/library/detail/site-detail-page.tsx");
  assert.match(detail, /aria-haspopup="menu"/u);
  assert.match(detail, /删除收藏…/u);
  assert.match(detail, /onDelete=\{\(\) => openDialog\("delete"\)\}/u);
  assert.match(detail, /确认删除/u);
  // 操作行：打开 / 星标 / 已看 / 推迟 / 编辑
  for (const label of ["打开", "星标", "已看", "推迟", "编辑"]) {
    assert.ok(
      new RegExp(`(?:\\n\\s*${label}\\n|"${label}")`, "u").test(detail),
      `操作行缺少「${label}」`,
    );
  }
  assert.match(detail, /<TaxonomyEditor site=\{site\} onUpdate=\{handleInlineUpdate\} \/>/u);
  assert.match(detail, /<RepoPanel site=\{site\} now=\{now\} \/>/u);
  assert.match(detail, /<UsageCard site=\{site\} now=\{now\} \/>/u);
  assert.match(detail, /<RelatedSitesCard/u);
});

test("the repo panel shows its notice instead of the stats grid (Q52)", async () => {
  const panel = await read("components/library/detail/repo-panel.tsx");
  assert.match(panel, /model\.notice \? \(/u);
  assert.match(panel, /<p className="sd-repo-notice" data-notice=\{model\.notice\.kind\}>/u);
  assert.match(panel, /\{model\.notice\.text\}/u);
});

test("the library management mode exposes kind, state and Space filters next to the existing tools", async () => {
  const [workspace, hook] = await Promise.all([
    read("components/library/library-workspace.tsx"),
    read("components/library/use-library-workspace.tsx"),
  ]);
  assert.match(workspace, /aria-label="按类型筛选"/u);
  assert.match(workspace, /aria-label="按阅读状态筛选"/u);
  assert.match(workspace, /ariaLabel="按 Space 筛选"/u);
  assert.match(workspace, /\{ value: "opened", label: "最近打开" \}/u);
  assert.match(workspace, /aria-pressed=\{viewMode === "list"\}/u);
  // 原有能力保留：批量选择与删除、相似排查、批量补全、置顶区、自定义顺序
  assert.match(workspace, /批量选择/u);
  assert.match(workspace, /删除所选/u);
  assert.match(workspace, /<SiteSimilarityReview/u);
  assert.match(workspace, /<HomeMetadataBackfillToolbar/u);
  assert.match(workspace, /<PinnedSites/u);
  assert.match(workspace, /\{ value: "custom", label: "自定义顺序" \}/u);

  assert.match(hook, /\.\.\.\(spaceId \? \{ spaceId \} : \{\}\)/u);
  assert.match(hook, /\.\.\.\(kind \? \{ kind \} : \{\}\)/u);
  assert.match(hook, /\.\.\.\(siteState \? \{ state: siteState \} : \{\}\)/u);
  assert.match(hook, /subscribeSiteUpdated\(\(site\) => \{[\s\S]*?withSiteUsage\(item, site\)/u);
});

test("backfill toolbar starts collapsed and pinned chips stay compact", async () => {
  const toolbar = await readFile(new URL("../components/home/home-metadata-backfill-toolbar.tsx", import.meta.url), "utf8");
  assert.match(toolbar, /useState\(false\)/u);
  assert.match(toolbar, /aria-expanded=\{expanded\}/u);
  assert.match(toolbar, /\{expanded && \(/u);
  const workspace = await readFile(new URL("../components/library/library-workspace.tsx", import.meta.url), "utf8");
  assert.match(workspace, /shouldUsePinnedChips\(/u);
  const header = await readFile(new URL("../app/styles/header.css", import.meta.url), "utf8");
  assert.match(header, /\.app-nav \{[^}]*min-width: 0;[^}]*overflow-x: auto;[^}]*scrollbar-width: none;/u);
});

test("detail page: repo cover carries the name, summary appears once, hint lives in the analyze button", async () => {
  const page = await read("components/library/detail/site-detail-page.tsx");
  assert.match(page, /<ResourceCover site=\{site\} size="hero" hideBlurb \/>/u);
  assert.match(page, /coverType === "repo" \? "sr-only" : "sd-title"/u);
  assert.doesNotMatch(page, /sd-desc-empty">\s*暂无详细介绍/u);
  assert.match(page, /aria-label=\{site\.description \? undefined : /u);
  const css = await read("app/styles/site-detail.css");
  assert.match(css, /\.sd-cover \{[^}]*height: clamp\(200px, 30vw, 280px\)/u);
});
