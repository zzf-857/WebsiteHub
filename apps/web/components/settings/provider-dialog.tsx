"use client";

import { X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode } from "react";

// providers.css 的 .provider-dialog-overlay 是常驻 display:flex 的遮罩层，
// 与原生 <dialog>（依赖 UA 的 :not([open]) { display:none }）互斥——
// 作者样式恒定胜过 UA 样式，用原生元素会导致弹层永远可见。
// 因此这里按已有类名走「条件渲染的 div 遮罩」，焦点陷阱与 Esc 自己实现。
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

type ProviderDialogProps = {
  open: boolean;
  title: string;
  description?: string;
  busy?: boolean;
  children: ReactNode;
  onClose: () => void;
};

export function ProviderDialog({
  open,
  title,
  description,
  busy = false,
  children,
  onClose,
}: Readonly<ProviderDialogProps>) {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    if (!panel) return;

    restoreFocusRef.current = document.activeElement as HTMLElement | null;
    // 先聚焦到第一个可交互控件，没有就退回面板本身（.provider-dialog 已设 outline:none）。
    const first = panel.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? panel).focus();

    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";

    return () => {
      document.body.style.overflow = overflow;
      restoreFocusRef.current?.focus?.();
    };
  }, [open]);

  if (!open) return null;

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      // 提交进行中不允许关闭：半途关掉会让用户以为操作已取消。
      if (!busy) onClose();
      return;
    }
    if (event.key !== "Tab") return;

    const panel = panelRef.current;
    if (!panel) return;
    const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === panel)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      className="provider-dialog-overlay"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <div
        ref={panelRef}
        className="provider-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="provider-dialog-head">
          <h2 className="provider-dialog-title" id={titleId}>{title}</h2>
          <button
            className="provider-dialog-close"
            type="button"
            disabled={busy}
            onClick={onClose}
            aria-label="关闭"
            title="关闭"
          >
            <X aria-hidden="true" />
          </button>
        </header>
        {description && (
          <p className="provider-dialog-text" id={descriptionId}>{description}</p>
        )}
        {children}
      </div>
    </div>
  );
}
