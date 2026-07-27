import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const THREAD = new URL("../components/agent/conversation-thread.tsx", import.meta.url);
const LAYOUT = new URL("../app/layout.tsx", import.meta.url);
const GLOBALS = new URL("../app/globals.css", import.meta.url);

test("assistant prose uses streaming Markdown without exposing raw tool JSON", async () => {
  const [thread, layout, globals] = await Promise.all([
    readFile(THREAD, "utf8"),
    readFile(LAYOUT, "utf8"),
    readFile(GLOBALS, "utf8"),
  ]);

  assert.match(thread, /<Streamdown/u);
  assert.match(thread, /mode=\{streaming \? "streaming" : "static"\}/u);
  assert.match(thread, /disallowedElements=\{\["img"\]\}/u);
  assert.match(thread, /className="chat-link-card"/u);
  assert.match(thread, /className="chat-reasoning"/u);
  assert.doesNotMatch(thread, /tool-card-raw|JSON\.stringify|<pre className=/u);
  assert.match(layout, /import "streamdown\/styles\.css"/u);
  assert.match(globals, /@source "\.\.\/node_modules\/streamdown\/dist\/\*\.js"/u);
});

test("saved tool links navigate to WebHub detail pages", async () => {
  const thread = await readFile(THREAD, "utf8");
  assert.match(thread, /href=\{`\/library\/\$\{encodeURIComponent\(item\.siteId\)\}`\}/u);
  assert.match(thread, /href=\{`\/library\/\$\{encodeURIComponent\(draft\.before\.siteId\)\}`\}/u);
  assert.match(thread, /<SiteFavicon url=\{item\.faviconUrl\}/u);
});
