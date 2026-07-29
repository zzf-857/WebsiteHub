"use client";

import { X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

const DIALOG_TRANSITION_MS = 160;

type LibraryDialogProps = {
  open: boolean;
  title: string;
  description?: string;
  size?: "standard" | "wide";
  closeDisabled?: boolean;
  children: ReactNode;
  onClose: () => void;
};

export function LibraryDialog({
  open,
  title,
  description,
  size = "standard",
  closeDisabled = false,
  children,
  onClose,
}: Readonly<LibraryDialogProps>) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeTimerRef = useRef<number | null>(null);
  const backdropPointerDownRef = useRef(false);
  const closingRef = useRef(false);
  const openRef = useRef(open);
  const closeDisabledRef = useRef(closeDisabled);
  const onCloseRef = useRef(onClose);
  const [snapshot, setSnapshot] = useState({ title, description, children });
  const [closing, setClosing] = useState(false);
  const titleId = useId();
  const descriptionId = useId();

  useLayoutEffect(() => {
    openRef.current = open;
    closeDisabledRef.current = closeDisabled;
    onCloseRef.current = onClose;
  }, [closeDisabled, onClose, open]);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => {
      setSnapshot({ title, description, children });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [children, description, open, title]);

  const displayed = open
    ? { title, description, children }
    : snapshot;

  const beginClose = useCallback(() => {
    const dialog = dialogRef.current;
    if (!dialog?.open) return;
    if (closingRef.current) return;

    closingRef.current = true;
    setClosing(true);
    const closeDelay = window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ? 0
      : DIALOG_TRANSITION_MS;
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null;
      if (dialogRef.current?.open) dialogRef.current.close();
    }, closeDelay);
  }, []);

  const requestClose = useCallback(() => {
    if (closeDisabledRef.current || closingRef.current) return;
    // 这是受控弹窗：先让父组件改变 open，再由 effect 播放退场动画。
    // 不能先调用原生 close()，否则父组件拒绝关闭时会产生状态分裂。
    onCloseRef.current();
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      if (closeTimerRef.current !== null) {
        window.clearTimeout(closeTimerRef.current);
        closeTimerRef.current = null;
      }
      closingRef.current = false;
      const readyFrame = window.requestAnimationFrame(() => {
        if (openRef.current) setClosing(false);
      });
      if (!dialog.open) dialog.showModal();
      return () => window.cancelAnimationFrame(readyFrame);
    }
    if (dialog.open) beginClose();
  }, [beginClose, open]);

  useEffect(() => () => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
    }
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="library-dialog"
      data-size={size}
      data-state={closing ? "closing" : "open"}
      aria-labelledby={titleId}
      aria-describedby={displayed.description ? descriptionId : undefined}
      onCancel={(event) => {
        event.preventDefault();
        requestClose();
      }}
      onClose={() => {
        if (closeTimerRef.current !== null) {
          window.clearTimeout(closeTimerRef.current);
          closeTimerRef.current = null;
        }
        closingRef.current = false;
        setClosing(false);

        // 防御原生 requestClose()/表单 method=dialog 等意外关闭路径。
        // busy 时恢复弹窗；可关闭时则同步受控父状态。
        if (!openRef.current) return;
        if (!closeDisabledRef.current) onCloseRef.current();
        window.queueMicrotask(() => {
          const dialog = dialogRef.current;
          if (openRef.current && dialog && !dialog.open) dialog.showModal();
        });
      }}
      onPointerDown={(event) => {
        backdropPointerDownRef.current = event.target === event.currentTarget;
      }}
      onPointerCancel={() => {
        backdropPointerDownRef.current = false;
      }}
      onClick={(event) => {
        const backdropClick =
          backdropPointerDownRef.current && event.target === event.currentTarget;
        backdropPointerDownRef.current = false;
        if (backdropClick) requestClose();
      }}
    >
      <div className="library-dialog-surface" inert={closing || undefined}>
        <header className="library-dialog-header">
          <div>
            <h2 id={titleId}>{displayed.title}</h2>
            {displayed.description && <p id={descriptionId}>{displayed.description}</p>}
          </div>
          <button
            className="icon-button"
            type="button"
            disabled={closing || closeDisabled}
            onClick={requestClose}
            aria-label="关闭"
            title={closeDisabled ? "操作完成后可关闭" : "关闭"}
          >
            <X aria-hidden="true" />
          </button>
        </header>
        <div className="library-dialog-body">{displayed.children}</div>
      </div>
    </dialog>
  );
}
