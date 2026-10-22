"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { ShortcutItemCommand } from "@/lib/keyboard-shortcuts";

// 快捷键与具体收藏之间的约定：
// - 画廊 / 列表里的每一项带 data-shortcut-item（瀑布流另带 data-shortcut-order 表示数据顺序），
//   j / k 在这些元素之间移动焦点，焦点落在其中的 [data-shortcut-focus] 上；
// - 详情页这类“单条收藏”的页面带 data-shortcut-default，焦点不在任何一项上时 o / s / e / z 作用于它；
// - 全局分发器把命令以 CustomEvent 派发到目标元素上，由持有收藏数据的组件自己执行。
//   这样分发器不需要知道任何收藏数据，卡片也不需要知道按键。

export const SHORTCUT_EVENT = "webhub:shortcut";

export type ShortcutEventDetail = { command: ShortcutItemCommand };

export function dispatchShortcut(target: HTMLElement, command: ShortcutItemCommand): void {
  target.dispatchEvent(
    new CustomEvent<ShortcutEventDetail>(SHORTCUT_EVENT, { detail: { command } }),
  );
}

/**
 * 让一个元素接收 o / s / e / z。返回的回调 ref 挂到带 data-shortcut-item 或
 * data-shortcut-default 的元素上；handler 总是读最新闭包，不需要稳定引用。
 */
export function useShortcutTarget<T extends HTMLElement>(
  handler: (command: ShortcutItemCommand) => void,
): (element: T | null) => void {
  const [element, setElement] = useState<T | null>(null);
  const handlerRef = useRef(handler);

  useLayoutEffect(() => {
    handlerRef.current = handler;
  });

  useEffect(() => {
    if (!element) return;
    const listener = (event: Event) => {
      const detail = (event as CustomEvent<ShortcutEventDetail>).detail;
      if (detail) handlerRef.current(detail.command);
    };
    element.addEventListener(SHORTCUT_EVENT, listener);
    return () => element.removeEventListener(SHORTCUT_EVENT, listener);
  }, [element]);

  return setElement;
}
