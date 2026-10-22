"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { useLibrarySearchTransition } from "@/components/library-search-transition";
import { LibraryDialog } from "@/components/library/library-dialog";
import {
  INITIAL_SHORTCUT_STATE,
  isShortcutItemCommand,
  isTypingTarget,
  nextFocusIndex,
  orderShortcutItems,
  resolveShortcut,
  SHORTCUT_HELP,
  type ShortcutCommand,
} from "@/lib/keyboard-shortcuts";

import { dispatchShortcut } from "./shortcut-target";

// 全站快捷键分发器（Q46）。挂在工作区布局里，只有一个 keydown 监听：
// 决策交给 lib/keyboard-shortcuts 的纯函数，这里只负责读 DOM、跳转与打开帮助浮层。

export const OPEN_SHORTCUT_HELP_EVENT = "webhub:shortcut-help";

/** 账号菜单等处用它打开快捷键说明（不必模拟按键）。 */
export function openShortcutHelp(): void {
  window.dispatchEvent(new Event(OPEN_SHORTCUT_HELP_EVENT));
}

const GO_ROUTES: Partial<Record<ShortcutCommand, string>> = {
  "go-discover": "/",
  "go-library": "/library",
  "go-spaces": "/spaces",
};

function visibleShortcutItems(): HTMLElement[] {
  const items = Array.from(document.querySelectorAll<HTMLElement>("[data-shortcut-item]"))
    .filter((item) => item.getClientRects().length > 0);
  return orderShortcutItems(items, (item) => {
    const raw = item.dataset.shortcutOrder;
    return raw === undefined || raw === "" ? null : Number(raw);
  });
}

/** 模态弹层，或标记了 data-shortcut-block 的浮层（选择器、“更多”菜单）打开时，快捷键让路。 */
function modalOpen(): boolean {
  return document.querySelector("dialog[open], [aria-modal='true'], [data-shortcut-block]") !== null;
}

function focusShortcutItem(item: HTMLElement): void {
  const target = item.querySelector<HTMLElement>("[data-shortcut-focus]") ?? item;
  target.focus({ preventScroll: true });
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView?.({ block: "nearest", behavior: reduced ? "auto" : "smooth" });
}

export function GlobalShortcuts() {
  const router = useRouter();
  const { start: startSearch } = useLibrarySearchTransition();
  const [helpOpen, setHelpOpen] = useState(false);
  const stateRef = useRef(INITIAL_SHORTCUT_STATE);
  const helpOpenRef = useRef(false);

  useEffect(() => {
    helpOpenRef.current = helpOpen;
  }, [helpOpen]);

  const run = useCallback(
    (command: ShortcutCommand, items: HTMLElement[], currentIndex: number) => {
      if (command === "help") {
        setHelpOpen(true);
        return;
      }
      if (command === "search") {
        // 键盘用户已经准备输入：不播放长距离转场，直接聚焦搜索框
        startSearch({ animate: false, trigger: "keyboard" });
        return;
      }
      const route = GO_ROUTES[command];
      if (route) {
        router.push(route);
        return;
      }
      if (command === "focus-next" || command === "focus-prev") {
        const index = nextFocusIndex(currentIndex, items.length, command);
        const item = items[index];
        if (item) focusShortcutItem(item);
        return;
      }
      if (isShortcutItemCommand(command)) {
        const target = items[currentIndex]
          ?? document.querySelector<HTMLElement>("[data-shortcut-default]");
        if (target) dispatchShortcut(target, command);
      }
    },
    [router, startSearch],
  );

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const active = document.activeElement;
      const target = event.target instanceof HTMLElement ? event.target : active;
      const items = visibleShortcutItems();
      const currentIndex = active ? items.findIndex((item) => item.contains(active)) : -1;
      const resolution = resolveShortcut(stateRef.current, event, {
        now: Date.now(),
        typing: isTypingTarget(target as HTMLElement | null),
        blocked: helpOpenRef.current || modalOpen(),
        itemFocused: currentIndex >= 0,
      });
      stateRef.current = resolution.state;
      if (resolution.preventDefault) event.preventDefault();
      if (resolution.command) run(resolution.command, items, currentIndex);
    };
    const handleOpenHelp = () => setHelpOpen(true);

    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener(OPEN_SHORTCUT_HELP_EVENT, handleOpenHelp);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener(OPEN_SHORTCUT_HELP_EVENT, handleOpenHelp);
    };
  }, [run]);

  return (
    <LibraryDialog
      open={helpOpen}
      title="键盘快捷键"
      description="在输入框里打字时，单键快捷键不会生效。"
      onClose={() => setHelpOpen(false)}
    >
      <div className="shortcut-help">
        {SHORTCUT_HELP.map((group) => (
          <section className="shortcut-help-group" key={group.title}>
            <h3>{group.title}</h3>
            <dl>
              {group.entries.map((entry) => (
                <div className="shortcut-help-row" key={entry.label}>
                  <dt>
                    {entry.keys.map((key, index) => (
                      <kbd key={`${key}-${index}`}>{key}</kbd>
                    ))}
                  </dt>
                  <dd>{entry.label}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </LibraryDialog>
  );
}
