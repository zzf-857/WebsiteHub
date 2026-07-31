import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const THREAD = new URL("../components/agent/conversation-thread.tsx", import.meta.url);
const PANEL_HOOK = new URL("../components/agent/use-agent-panel.tsx", import.meta.url);
const RESULT_CARDS = new URL("../components/agent/agent-result-cards.tsx", import.meta.url);
const RESULT_PAGINATION = new URL("../components/agent/agent-result-pagination.tsx", import.meta.url);
const LAYOUT = new URL("../app/layout.tsx", import.meta.url);
const GLOBALS = new URL("../app/globals.css", import.meta.url);

test("assistant prose uses streaming Markdown without exposing raw tool JSON", async () => {
  const [thread, panelHook, resultCards, layout, globals] = await Promise.all([
    readFile(THREAD, "utf8"),
    readFile(PANEL_HOOK, "utf8"),
    readFile(RESULT_CARDS, "utf8"),
    readFile(LAYOUT, "utf8"),
    readFile(GLOBALS, "utf8"),
  ]);

  assert.match(thread, /<Streamdown/u);
  assert.match(thread, /mode=\{streaming \? "streaming" : "static"\}/u);
  assert.match(thread, /disallowedElements=\{STREAM_DISALLOWED_ELEMENTS\}/u);
  assert.match(thread, /className="chat-link-card"/u);
  assert.match(thread, /className="chat-reasoning"/u);
  assert.match(thread, /part\.type === "source-url"/u);
  assert.match(thread, /使用 \{unique\.length\} 个来源/u);
  assert.match(thread, /<SiteFavicon url=\{null\} name=\{title\} size=\{18\} \/>/u);
  assert.doesNotMatch(thread, /sourceFaviconUrl|\/favicon\.ico/u);
  assert.match(thread, /messageStatus !== "complete" \|\| metadata\.turnPersisted === false/u);
  assert.match(thread, /draftDisabled=\{draftDisabled\}/u);
  assert.match(thread, /result\.name === "present_website_recommendations"/u);
  assert.match(thread, /followOutputRef\.current = atBottom/u);
  assert.match(thread, /className="chat-return-bottom"/u);
  assert.match(thread, /scrollToBottom\("auto"\)/u);
  assert.doesNotMatch(thread, /scrollIntoView/u);
  assert.match(thread, /输入 \{usage\.inputTokens/u);
  assert.match(thread, /思考 \{usage\.reasoningTokens/u);
  assert.match(thread, /usage\?\.totalTokens !== undefined/u);
  assert.match(
    thread,
    /aria-label=\{answerFeedback === "copied"[\s\S]*?"回答已复制"[\s\S]*?answerFeedback === "error" \? "复制回答失败，请重试" : "复制回答"\}/u,
  );
  assert.match(
    thread,
    /aria-label=\{linkFeedback === "copied"[\s\S]*?"对话链接已复制"[\s\S]*?linkFeedback === "error" \? "复制对话链接失败，请重试" : "复制对话链接"\}/u,
  );
  assert.match(panelHook, /messageStatus: "aborted" as const/u);
  assert.match(panelHook, /return \[\.\.\.stages\.values\(\)\]/u);
  assert.match(panelHook, /current\.filter\(\(stage\) => stage\.status === "done"\)/u);
  assert.match(panelHook, /onError: handleStreamError/u);
  assert.match(panelHook, /assistant-aborted-\$\{userMessage\.id\}/u);
  assert.match(panelHook, /collectionDisabled/u);
  assert.match(resultCards, /disabled=\{collectionDisabled \|\| saved/u);
  assert.match(thread, /if \(!followOutputRef\.current\) return;/u);
  assert.match(thread, /durationMs \?\? liveDurationMs/u);
  assert.doesNotMatch(thread, /tool-card-raw|\{JSON\.stringify\(|<pre className=/u);
  assert.match(layout, /import "streamdown\/styles\.css"/u);
  assert.match(globals, /@source "\.\.\/node_modules\/streamdown\/dist\/\*\.js"/u);
});

test("saved tool links navigate to WebHub detail pages", async () => {
  const thread = await readFile(THREAD, "utf8");
  assert.match(thread, /href=\{`\/library\/\$\{encodeURIComponent\(item\.siteId\)\}`\}/u);
  assert.match(thread, /href=\{`\/library\/\$\{encodeURIComponent\(draft\.before\.siteId\)\}`\}/u);
  assert.match(thread, /<SiteFavicon url=\{item\.faviconUrl\}/u);
});

test("complete Agent results use numbered pagination with icon navigation", async () => {
  const pagination = await readFile(RESULT_PAGINATION, "utf8");

  assert.match(pagination, /agentResultPageSlice\(items, requestedPage\)/u);
  assert.match(pagination, /aria-label="上一页"/u);
  assert.match(pagination, /aria-label="下一页"/u);
  assert.match(pagination, /aria-current=\{token === page\.page \? "page" : undefined\}/u);
  assert.match(pagination, /<ChevronLeft/u);
  assert.match(pagination, /<ChevronRight/u);
});
