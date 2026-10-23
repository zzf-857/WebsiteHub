import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(new URL("../components/settings/data-portability-workspace.tsx", import.meta.url), "utf8");
const hook = readFileSync(new URL("../components/settings/use-data-portability.tsx", import.meta.url), "utf8");

test("backup workspace keeps download, file selection, preview and confirmation entry points", () => {
  assert.match(workspace, /href=\{PORTABILITY_EXPORT_URL\} download/u);
  assert.match(workspace, /accept="\.json,application\/json"/u);
  assert.match(workspace, /onDrop=/u);
  assert.match(workspace, /<PreviewSummary preview=\{state\.preview\}/u);
  assert.match(workspace, /<ProviderDialog open=\{state\.confirming\}/u);
  assert.match(workspace, /onClick=\{\(\) => state\.setConfirming\(true\)\}/u);
  assert.match(workspace, /href="\/library">去收藏库查看/u);
  assert.match(workspace, /role="alert"/u);
  assert.match(workspace, /role="status" aria-live="polite"/u);
  assert.match(hook, /sequence !== requestRef\.current\.sequence/u);
  assert.match(hook, /announceLibraryChanged\(\)/u);
});

test("backup samples remain text and navigation follows bookmark import", () => {
  assert.match(workspace, /<span>\{sample\.url\}<\/span>/u);
  assert.doesNotMatch(workspace, /href=\{sample\.url\}/u);
  const header = readFileSync(new URL("../components/site-header.tsx", import.meta.url), "utf8");
  assert.match(header, /label: "导入书签"[^\n]+\n\s*\{ label: "数据导出与恢复", href: "\/settings\/data", icon: DatabaseBackup/u);
});
