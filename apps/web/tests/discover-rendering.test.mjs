import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const ROOT = new URL("../", import.meta.url);
const read = (path) => readFile(new URL(path, ROOT), "utf8");

const NEW_TOKENS = [
  "--discover-max",
  "--cover-repo-bg",
  "--cover-repo-dot",
  "--cover-repo-title",
  "--cover-repo-muted",
  "--cover-repo-star",
  "--cover-article-bg",
  "--cover-article-text",
  "--cover-quote",
  "--cover-tint-l",
  "--cover-tint-c",
  "--cover-domain-l",
  "--cover-domain-c",
  "--cover-lang-l",
  "--cover-lang-c",
  "--cover-scrim",
  "--cover-badge-bg",
  "--cover-badge-text",
  "--cover-tile-shadow",
];

test("the home route is the discover page and the agent panel lives on /chat", async () => {
  const [home, chat, chatNew, chatConversation, chatPage, discover] = await Promise.all([
    read("app/(workspace)/page.tsx"),
    read("app/(workspace)/chat/page.tsx"),
    read("app/(workspace)/chat/new/page.tsx"),
    read("app/(workspace)/chat/[conversationId]/page.tsx"),
    read("components/agent/chat-page.tsx"),
    read("components/discover/discover-page.tsx"),
  ]);

  assert.match(home, /<DiscoverPage \/>/u);
  assert.doesNotMatch(home, /AgentPanel/u);
  assert.match(chat, /<ChatPage \/>/u);
  assert.match(chatPage, /<AgentPanel \/>/u);
  assert.match(chatNew, /redirect\("\/chat"\)/u);
  assert.match(chatConversation, /redirect\(`\/chat\?c=\$\{encodeURIComponent\(conversationId\)\}`\)/u);
  // 发现页只有两大区块；整库为空时换成首次使用面板
  assert.match(discover, /<RediscoverSection \/>/u);
  assert.match(discover, /<GallerySection overview=\{ready\} revision=\{revision\} \/>/u);
  assert.match(discover, /firstRun \? \(\s*<FirstRunPanel \/>/u);
  assert.match(discover, /href="\/settings\/import"/u);
  assert.match(discover, /href="\/settings\/agents"/u);
  assert.match(discover, /<h1 className="sr-only">发现<\/h1>/u);
});

test("the header carries discover navigation, quick add and the account menu entries", async () => {
  const header = await read("components/site-header.tsx");
  assert.match(header, /\{ label: "发现", href: "\/", sections: \["\/"\] \}/u);
  assert.match(header, /\{ label: "收藏库", href: "\/library"/u);
  assert.match(header, /\{ label: "Space", href: "\/spaces"/u);
  assert.doesNotMatch(header, /网址库/u);
  assert.match(header, /<QuickAddButton \/>/u);
  for (const href of ["/settings/providers", "/settings/agents", "/settings/import", "/settings/search-index", "/chat"]) {
    assert.match(header, new RegExp(`href: "${href.replace(/\//gu, "\\/")}"`, "u"), `账号菜单缺少 ${href}`);
  }
  assert.match(header, /aria-pressed=\{auth\.theme === value\}/u);
  assert.match(header, /handleLogout/u);
});

test("quick add validates inline, reports duplicates and refreshes discover data", async () => {
  const quickAdd = await read("components/discover/quick-add.tsx");
  assert.match(quickAdd, /parseQuickAddUrl\(value\)/u);
  assert.match(quickAdd, /createLibrarySite\(\{ name: parsed\.name, url: parsed\.url \}\)/u);
  assert.match(quickAdd, /announceLibraryChanged\(\)/u);
  assert.match(quickAdd, /"已收藏，正在整理…"/u);
  assert.match(quickAdd, /"已经收藏过了"/u);
  assert.match(quickAdd, /duplicateSiteHref\(parsed\.url\)/u);
  assert.match(quickAdd, /role="alert"/u);
  assert.match(quickAdd, /event\.key !== "Escape"/u);
  assert.match(quickAdd, /aria-expanded=\{open\}/u);
});

test("the hero opens the original site synchronously and offers undo for reversible actions", async () => {
  const [hero, actions] = await Promise.all([
    read("components/discover/rediscover-section.tsx"),
    read("components/discover/use-site-actions.ts"),
  ]);
  assert.match(actions, /window\.open\(site\.originalUrl, "_blank", "noopener"\)/u);
  assert.match(actions, /recordSiteOpen\(site\.id\)/u);
  assert.match(hero, /fetchRediscover\(\{ limit: REDISCOVER_LIMIT, shuffle \}/u);
  assert.match(hero, /label: "撤销"/u);
  assert.match(hero, /heroUndoInput\(dismissal\)/u);
  assert.match(hero, /<AnimatePresence mode="wait" initial=\{false\}>/u);
  assert.match(hero, /收藏都看过了。新收藏满 3 天后，没打开过的会出现在这里。/u);
});

test("resource cards keep quick actions outside the detail link", async () => {
  const card = await read("components/discover/resource-card.tsx");
  const linkEnd = card.indexOf("</Link>");
  const overlay = card.indexOf('className="resource-card-overlay"');
  assert.ok(linkEnd > 0 && overlay > linkEnd, "遮罩与快捷操作必须在链接之外");
  assert.match(card, /aria-pressed=\{site\.starred\}/u);
  assert.match(card, /site\.source === "agent"/u);
  assert.match(card, /未打开/u);

  const cover = await read("components/discover/resource-cover.tsx");
  assert.match(cover, /referrerPolicy="no-referrer"/u);
  assert.match(cover, /loading=\{size === "hero" \? "eager" : "lazy"\}/u);
  assert.match(cover, /onError=\{\(\) => setFailedSrc\(model\.src\)\}/u);
  assert.match(cover, /"--site-hue": hue/u);
});

test("discover styles use tokens only, radii stay within 8px and motion can be reduced", async () => {
  const [styles, globals] = await Promise.all([read("app/styles/discover.css"), read("app/globals.css")]);

  assert.doesNotMatch(styles, /#[0-9a-f]{3,8}\b/iu, "颜色必须来自令牌");
  assert.doesNotMatch(styles, /\brgba?\(/iu, "颜色必须来自令牌");
  for (const match of styles.matchAll(/border-radius:\s*(\d+)px/gu)) {
    assert.ok(Number(match[1]) <= 8, `圆角 ${match[1]}px 超过 8px`);
  }
  assert.match(styles, /@media \(prefers-reduced-motion: reduce\)/u);
  assert.match(styles, /@media \(hover: none\)/u);
  assert.match(styles, /oklch\(var\(--cover-tint-l\) var\(--cover-tint-c\) var\(--site-hue\)\)/u);
  assert.match(styles, /grid-template-rows: subgrid/u);

  for (const token of NEW_TOKENS) {
    const count = globals.split(`${token}:`).length - 1;
    assert.equal(count, 3, `${token} 需要同时定义在浅色、深色与跟随系统的深色块里`);
  }
  assert.match(globals, /@import "\.\/styles\/discover\.css";/u);
  assert.match(globals, /@media \(max-width: 767px\) \{\s*:root \{\s*--page-gutter: 16px;/u);
});

test("the library keeps the moved backfill toolbar and pinned section under the new title", async () => {
  const library = await read("components/library/library-workspace.tsx");
  assert.match(library, /<h1>收藏库<\/h1>/u);
  assert.match(library, /<HomeMetadataBackfillToolbar/u);
  // 补全工具条与置顶区是兄弟节点，各自的重挂 key 必须带前缀，避免同为 0 时冲突
  assert.match(library, /key=\{`backfill-\$\{backfillToolbarKey\}`\}/u);
  assert.match(library, /<PinnedSites key=\{`pinned-\$\{pinnedKey\}`\} \/>/u);

  const intent = await read("components/library/library-workspace-parts.tsx");
  assert.match(intent, /query: params\.get\("q"\)/u);
  const helpCard = await read("components/library/detail/agent-help-card.tsx");
  assert.match(helpCard, /href=\{`\/chat\?ask=\$\{encodeURIComponent\(text\)\}`\}/u);
});
