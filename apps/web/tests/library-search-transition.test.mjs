import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const ROOT = new URL("../", import.meta.url);
const TRANSITION = new URL("components/library-search-transition.tsx", ROOT);
const WORKSPACE_LAYOUT = new URL("app/(workspace)/layout.tsx", ROOT);
const SITE_HEADER = new URL("components/site-header.tsx", ROOT);
const LIBRARY_WORKSPACE = new URL("components/library/library-workspace.tsx", ROOT);
const MOTION_STYLES = new URL("app/styles/motion.css", ROOT);

test("library search navigation is coordinated by a persistent shared-element provider", async () => {
  const [transition, layout] = await Promise.all([
    readFile(TRANSITION, "utf8"),
    readFile(WORKSPACE_LAYOUT, "utf8"),
  ]);

  assert.match(layout, /<LibrarySearchTransitionProvider>/u);
  assert.match(layout, /<SiteHeader \/>/u);
  assert.match(transition, /<LayoutGroup id="library-search-transition">/u);
  assert.match(transition, /<MotionConfig reducedMotion="user"/u);
  assert.match(transition, /router\.prefetch\(ROOT_LIBRARY_PATH\)/u);
  assert.match(transition, /LIBRARY_LAYOUT_STORAGE_KEY/u);
  assert.match(transition, /dataset\.librarySearchLayout/u);
  assert.match(transition, /window\.requestAnimationFrame\(\(\) => \{/u);
  assert.match(transition, /router\.push\(ROOT_LIBRARY_SEARCH_PATH\)/u);
  assert.match(transition, /lockedRef\.current/u);
  assert.match(transition, /TRANSITION_TIMEOUT_MS/u);
  assert.match(transition, /const handleTimeout = useCallback/u);
  const timeoutStart = transition.indexOf("const handleTimeout");
  const timeoutEnd = transition.indexOf("const armTimeout", timeoutStart);
  const timeoutBody = transition.slice(timeoutStart, timeoutEnd);
  assert.ok(
    timeoutBody.indexOf("router.push(ROOT_LIBRARY_SEARCH_PATH)")
      < timeoutBody.indexOf("finish();"),
    "timeout must issue a suspended navigation before settling the transition",
  );
  assert.match(transition, /clearDocumentTransitionState\(\)/u);
});

test("the header and library toolbar hand off one layout id without duplicate library entry", async () => {
  const [header, library] = await Promise.all([
    readFile(SITE_HEADER, "utf8"),
    readFile(LIBRARY_WORKSPACE, "utf8"),
  ]);

  assert.match(header, /isLibrarySection \? null : compact \?/u);
  assert.match(header, /<motion\.button/u);
  assert.match(header, /layoutId=\{searchTransitionActive \? LIBRARY_SEARCH_LAYOUT_ID : undefined\}/u);
  assert.match(header, /animate: trigger === "pointer"/u);
  assert.match(header, /event\.detail === 0 \? "keyboard" : "pointer"/u);
  assert.match(header, /openSearch\("keyboard"\)/u);
  assert.doesNotMatch(header, /router\.push\("\/library\?focus=search"\)/u);

  assert.match(library, /<motion\.label/u);
  assert.match(library, /layoutId=\{searchTransitionActive \? LIBRARY_SEARCH_LAYOUT_ID : undefined\}/u);
  assert.match(library, /registerSearchTarget\(target\)/u);
  assert.match(library, /onLayoutAnimationComplete/u);
  assert.match(library, /finishSearchTransition\(\)/u);
  assert.match(library, /data-library-search-input/u);
  assert.match(library, /LIBRARY_LAYOUT_STORAGE_KEY/u);
});

test("shared search motion keeps surrounding content subordinate and respects reduced motion", async () => {
  const styles = await readFile(MOTION_STYLES, "utf8");

  assert.match(styles, /data-library-search-transition="active"/u);
  assert.match(styles, /library-search-content-arrival/u);
  assert.match(styles, /library-search-field\[data-search-transition-target\]/u);
  assert.match(styles, /z-index: 60/u);
  assert.match(styles, /@media \(prefers-reduced-motion: reduce\)/u);
  assert.match(styles, /animation: none !important/u);
});
