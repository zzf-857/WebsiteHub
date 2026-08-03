import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const ROOT = new URL("../", import.meta.url);
const SELECT = new URL("components/ui/themed-select.tsx", ROOT);
const CONTROLS = new URL("app/styles/controls.css", ROOT);
const GLOBALS = new URL("app/globals.css", ROOT);
const AGENT_PANEL = new URL("components/agent/agent-panel.tsx", ROOT);
const AGENT_HOOK = new URL("components/agent/use-agent-panel.tsx", ROOT);
const AGENT_STYLES = new URL("app/styles/agent-panel.css", ROOT);
const SITE_HEADER = new URL("components/site-header.tsx", ROOT);

const SELECT_CONSUMERS = [
  "components/home/category-sections.tsx",
  "components/library/library-workspace.tsx",
  "components/library/site-form.tsx",
  "components/settings/provider-form.tsx",
  "components/spaces/space-workspace.tsx",
];

test("business controls use the shared themed selector instead of platform popovers", async () => {
  const sources = await Promise.all(
    SELECT_CONSUMERS.map(async (path) => [path, await readFile(new URL(path, ROOT), "utf8")]),
  );

  for (const [path, source] of sources) {
    assert.match(source, /ThemedSelect/u, `${path} should use ThemedSelect`);
    assert.doesNotMatch(source, /<(?:select|option|datalist)\b/u, `${path} must not restore a native selector`);
  }

  const provider = sources.find(([path]) => path.endsWith("provider-form.tsx"))?.[1] ?? "";
  assert.match(provider, /searchable/u);
  assert.doesNotMatch(provider, /\blist=\{/u);
  assert.match(provider, /className="provider-checkbox-control"/u);
});

test("ThemedSelect keeps keyboard, disabled-option, portal and dialog contracts", async () => {
  const [select, controls, globals] = await Promise.all([
    readFile(SELECT, "utf8"),
    readFile(CONTROLS, "utf8"),
    readFile(GLOBALS, "utf8"),
  ]);

  assert.match(select, /role="combobox"/u);
  assert.match(select, /role="listbox"/u);
  assert.match(select, /role="option"/u);
  assert.match(select, /aria-activedescendant/u);
  assert.match(select, /aria-disabled=\{option\.disabled/u);
  assert.match(select, /case "ArrowDown"/u);
  assert.match(select, /case "ArrowUp"/u);
  assert.match(select, /case "Home"/u);
  assert.match(select, /case "End"/u);
  assert.match(select, /case "Enter"/u);
  assert.match(select, /event\.key === "Escape"/u);
  assert.match(select, /event\.key === "Tab"/u);
  assert.match(select, /event\.stopPropagation\(\)/u);
  assert.match(select, /focusAdjacentControl\(event\.shiftKey\)/u);
  assert.match(select, /createPortal\(popup, portalTarget\)/u);
  assert.match(select, /closest\("dialog"\)/u);
  assert.match(select, /ResizeObserver/u);
  assert.match(select, /visualViewport/u);

  assert.match(globals, /@import "\.\/styles\/controls\.css"/u);
  assert.match(controls, /\.themed-select-popover/u);
  assert.match(controls, /var\(--surface-raised\)/u);
  assert.match(controls, /var\(--focus\)/u);
  assert.match(controls, /@media \(pointer: coarse\)/u);
  assert.match(controls, /@media \(prefers-reduced-motion: reduce\)/u);
});

test("Agent menus expose focus navigation and textbox-to-listbox ownership", async () => {
  const [panel, hook] = await Promise.all([
    readFile(AGENT_PANEL, "utf8"),
    readFile(AGENT_HOOK, "utf8"),
  ]);

  assert.match(panel, /ref=\{historyTriggerRef\}/u);
  assert.match(panel, /onKeyDown=\{handleHistoryMenuKeyDown\}/u);
  assert.match(panel, /aria-controls=\{commandPanelOpen \? commandPanelId : undefined\}/u);
  assert.match(panel, /aria-activedescendant=\{activeCommandOptionId\}/u);
  assert.match(hook, /\["ArrowDown", "ArrowUp", "Home", "End"\]/u);
  assert.match(hook, /window\.requestAnimationFrame\(\(\) => historyTriggerRef\.current\?\.focus\(\)\)/u);
  assert.match(hook, /tabIndex=\{-1\}/u);
});

test("the themed account popover closes with Escape and restores its trigger", async () => {
  const header = await readFile(SITE_HEADER, "utf8");

  assert.match(header, /event\.key !== "Escape" \|\| !details\?\.open/u);
  assert.match(header, /details\.removeAttribute\("open"\)/u);
  assert.match(header, /querySelector<HTMLElement>\("summary"\)\?\.focus\(\)/u);
});

test("the Agent composer uses one restrained outer focus treatment", async () => {
  const styles = await readFile(AGENT_STYLES, "utf8");

  assert.match(styles, /\.agent-composer:hover\s*\{[\s\S]*?var\(--line-strong\)/u);
  assert.match(styles, /\.agent-composer:focus-within\s*\{[\s\S]*?color-mix\(in srgb, var\(--accent-mid\) 42%/u);
  assert.match(styles, /color-mix\(in srgb, var\(--focus\) 8%, transparent\)/u);
  assert.match(styles, /\.agent-composer textarea:focus-visible\s*\{\s*outline: none;/u);
  assert.match(styles, /\.agent-followup textarea:focus-visible\s*\{\s*outline: none;/u);
  assert.match(styles, /@media \(forced-colors: active\)/u);
});
