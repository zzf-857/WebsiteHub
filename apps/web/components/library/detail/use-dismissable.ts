"use client";

import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

export type DismissReason = "outside" | "escape";

/**
 * 详情页小浮层（分类 / 标签选择器、“更多”菜单）的关闭约定：点浮层外关闭、Esc 关闭并把焦点还给触发按钮。
 * Esc 在捕获阶段处理并阻止继续传播，避免同一次按键再被别的监听当成快捷键。
 */
export function useDismissable(
  open: boolean,
  rootRef: RefObject<HTMLElement | null>,
  triggerRef: RefObject<HTMLElement | null>,
  onDismiss: (reason: DismissReason) => void,
): void {
  const dismissRef = useRef(onDismiss);
  useLayoutEffect(() => {
    dismissRef.current = onDismiss;
  });

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: PointerEvent) => {
      const root = rootRef.current;
      if (!root || (event.target instanceof Node && root.contains(event.target))) return;
      dismissRef.current("outside");
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      dismissRef.current("escape");
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [open, rootRef, triggerRef]);
}
