import assert from "node:assert/strict";
import test from "node:test";

import {
  INITIAL_SHORTCUT_STATE,
  isShortcutItemCommand,
  isTypingTarget,
  nextFocusIndex,
  orderShortcutItems,
  resolveShortcut,
  SHORTCUT_HELP,
  SHORTCUT_SEQUENCE_TIMEOUT_MS,
  type ShortcutContext,
  type ShortcutKeyEvent,
  type ShortcutState,
} from "../lib/keyboard-shortcuts.ts";

const idle: ShortcutContext = { now: 10_000, typing: false, blocked: false, itemFocused: false };

function press(
  event: ShortcutKeyEvent,
  context: Partial<ShortcutContext> = {},
  state: ShortcutState = INITIAL_SHORTCUT_STATE,
) {
  return resolveShortcut(state, event, { ...idle, ...context });
}

/** 依次按下一串键，返回每一键的命令；时间按 `gap` 毫秒推进。 */
function sequence(keys: string[], gap = 100, context: Partial<ShortcutContext> = {}) {
  let state = INITIAL_SHORTCUT_STATE;
  let now = idle.now;
  return keys.map((key) => {
    const result = press({ key }, { ...context, now }, state);
    state = result.state;
    now += gap;
    return result.command;
  });
}

test("⌘K, Ctrl+K and / open search and prevent the browser default", () => {
  for (const event of [{ key: "k", metaKey: true }, { key: "K", ctrlKey: true }, { key: "/" }]) {
    const result = press(event);
    assert.equal(result.command, "search");
    assert.equal(result.preventDefault, true);
  }
  // ⌘⌥K 不是搜索
  assert.equal(press({ key: "k", metaKey: true, altKey: true }).command, null);
});

test("single-key shortcuts map to their commands", () => {
  assert.equal(press({ key: "j" }).command, "focus-next");
  assert.equal(press({ key: "k" }).command, "focus-prev");
  assert.equal(press({ key: "o" }).command, "open");
  assert.equal(press({ key: "s" }).command, "star");
  assert.equal(press({ key: "e" }).command, "seen");
  assert.equal(press({ key: "z" }).command, "snooze");
  assert.equal(press({ key: "?", shiftKey: true }).command, "help");
  assert.equal(press({ key: "x" }).command, null);
  assert.equal(press({ key: "x" }).preventDefault, false, "无关按键不拦截");
});

test("typing, IME composition and open dialogs swallow every page shortcut", () => {
  for (const key of ["j", "k", "o", "s", "e", "z", "?", "/", "g"]) {
    assert.equal(press({ key }, { typing: true }).command, null, `typing ${key}`);
    assert.equal(press({ key }, { blocked: true }).command, null, `blocked ${key}`);
    assert.equal(press({ key, isComposing: true }).command, null, `composing ${key}`);
  }
  // 与原顶栏一致：输入框里不抢 ⌘K
  assert.equal(press({ key: "k", metaKey: true }, { typing: true }).command, null);
  assert.equal(press({ key: "k", ctrlKey: true }, { blocked: true }).command, null);
  // 被吞掉的按键不阻止默认行为（字母照常写进输入框）
  assert.equal(press({ key: "j" }, { typing: true }).preventDefault, false);
});

test("modifiers and shifted letters never trigger single-key shortcuts", () => {
  assert.equal(press({ key: "s", ctrlKey: true }).command, null, "Ctrl+S 留给浏览器");
  assert.equal(press({ key: "o", metaKey: true }).command, null);
  assert.equal(press({ key: "e", altKey: true }).command, null);
  assert.equal(press({ key: "S", shiftKey: true }).command, null);
  assert.equal(press({ key: "J", shiftKey: true }).command, null);
});

test("g d / g l / g s navigate; g + s does not star", () => {
  assert.deepEqual(sequence(["g", "d"]), [null, "go-discover"]);
  assert.deepEqual(sequence(["g", "l"]), [null, "go-library"]);
  assert.deepEqual(sequence(["g", "s"]), [null, "go-spaces"]);
  const prefix = press({ key: "g" });
  assert.equal(prefix.state.pending, "g");
  assert.equal(prefix.preventDefault, true);
});

test("an unknown second key cancels the sequence and is re-read as a single key", () => {
  assert.deepEqual(sequence(["g", "j"]), [null, "focus-next"]);
  assert.deepEqual(sequence(["g", "x", "d"]), [null, null, null], "序列作废后 d 不再跳转");
  assert.deepEqual(sequence(["g", "g", "l"]), [null, null, "go-library"], "g g 重新开始序列");
});

test("a sequence expires after the timeout", () => {
  const late = SHORTCUT_SEQUENCE_TIMEOUT_MS + 1;
  assert.deepEqual(sequence(["g", "s"], late), [null, "star"], "超时后 s 是星标");
  assert.deepEqual(sequence(["g", "d"], SHORTCUT_SEQUENCE_TIMEOUT_MS), [null, "go-discover"]);
});

test("typing between g and the second key drops the sequence", () => {
  const prefix = press({ key: "g" });
  const typed = resolveShortcut(prefix.state, { key: "d" }, { ...idle, typing: true });
  assert.equal(typed.command, null);
  assert.equal(typed.state.pending, null);
});

test("arrow keys move focus only while an item already has focus", () => {
  assert.equal(press({ key: "ArrowDown" }).command, null, "否则保留页面滚动");
  assert.equal(press({ key: "ArrowDown" }).preventDefault, false);
  assert.equal(press({ key: "ArrowDown" }, { itemFocused: true }).command, "focus-next");
  assert.equal(press({ key: "ArrowRight" }, { itemFocused: true }).command, "focus-next");
  assert.equal(press({ key: "ArrowUp" }, { itemFocused: true }).command, "focus-prev");
  assert.equal(press({ key: "ArrowLeft" }, { itemFocused: true }).command, "focus-prev");
});

test("auto-repeat keeps moving focus but never re-toggles an item action", () => {
  assert.equal(press({ key: "j", repeat: true }).command, "focus-next");
  for (const key of ["o", "s", "e", "z"]) {
    const result = press({ key, repeat: true });
    assert.equal(result.command, null, key);
    assert.equal(result.preventDefault, true, `${key} 按住不放也不触发浏览器行为`);
  }
  assert.equal(press({ key: "g", repeat: true }).state.pending, null);
});

test("item commands are exactly open / star / seen / snooze", () => {
  assert.deepEqual(
    ["open", "star", "seen", "snooze", "help", "search", "focus-next"].map((command) =>
      isShortcutItemCommand(command as never),
    ),
    [true, true, true, true, false, false, false],
  );
});

test("typing targets: text fields and key-owning widgets, not buttons or checkboxes", () => {
  const element = (tagName: string, extra: Record<string, unknown> = {}) => ({
    tagName,
    getAttribute: (name: string) => (name === "role" ? (extra.role as string) ?? null : null),
    ...extra,
  });
  assert.equal(isTypingTarget(element("INPUT", { type: "text" })), true);
  assert.equal(isTypingTarget(element("INPUT", { type: "search" })), true);
  assert.equal(isTypingTarget(element("input", { type: "" })), true);
  assert.equal(isTypingTarget(element("INPUT", { type: "checkbox" })), false);
  assert.equal(isTypingTarget(element("INPUT", { type: "radio" })), false);
  assert.equal(isTypingTarget(element("TEXTAREA")), true);
  assert.equal(isTypingTarget(element("SELECT")), true);
  assert.equal(isTypingTarget(element("DIV", { isContentEditable: true })), true);
  assert.equal(isTypingTarget(element("BUTTON", { role: "combobox" })), true);
  assert.equal(isTypingTarget(element("BUTTON", { role: "menuitem" })), true);
  assert.equal(isTypingTarget(element("DIV", { role: "listbox" })), true);
  assert.equal(isTypingTarget(element("BUTTON")), false);
  assert.equal(isTypingTarget(element("A")), false);
  assert.equal(isTypingTarget(null), false);
  assert.equal(isTypingTarget({}), false);
});

test("focus moves one step, starts from the edge and stops at the ends", () => {
  assert.equal(nextFocusIndex(-1, 5, "focus-next"), 0);
  assert.equal(nextFocusIndex(-1, 5, "focus-prev"), 4);
  assert.equal(nextFocusIndex(0, 5, "focus-next"), 1);
  assert.equal(nextFocusIndex(3, 5, "focus-prev"), 2);
  assert.equal(nextFocusIndex(4, 5, "focus-next"), 4, "到底不循环");
  assert.equal(nextFocusIndex(0, 5, "focus-prev"), 0, "到顶不循环");
  assert.equal(nextFocusIndex(9, 5, "focus-next"), 0, "越界当作没有焦点");
  assert.equal(nextFocusIndex(0, 0, "focus-next"), -1);
});

test("masonry items follow data order when every item carries one", () => {
  const items = [
    { id: "a", order: 0 },
    { id: "c", order: 2 },
    { id: "b", order: 1 },
    { id: "d", order: 3 },
  ];
  assert.deepEqual(orderShortcutItems(items, (item) => item.order).map((item) => item.id), [
    "a",
    "b",
    "c",
    "d",
  ]);
  // 有一项缺序号（例如收藏库的普通列表）就保留 DOM 顺序
  const mixed = [{ id: "x", order: 2 }, { id: "y", order: null }, { id: "z", order: 0 }];
  assert.deepEqual(orderShortcutItems(mixed, (item) => item.order).map((item) => item.id), [
    "x",
    "y",
    "z",
  ]);
  assert.deepEqual(orderShortcutItems([], () => 0), []);
});

test("the help overlay documents every key the dispatcher understands", () => {
  const documented = new Set(SHORTCUT_HELP.flatMap((group) => group.entries.map((entry) => entry.keys.join(" "))));
  for (const keys of ["⌘ K", "/", "g d", "g l", "g s", "?", "j", "k", "o", "s", "e", "z"]) {
    assert.ok(documented.has(keys), `帮助浮层缺少 ${keys}`);
  }
  // 文档里的每个单键都真的有命令
  for (const keys of documented) {
    const parts = keys.split(" ");
    if (parts[0] === "⌘") continue;
    const results = sequence(parts);
    assert.notEqual(results.at(-1), null, `${keys} 没有对应命令`);
  }
});
