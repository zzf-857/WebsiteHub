"use client";

import { CircleAlert } from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import Link from "next/link";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

// 全站唯一的底部居中提示条（2026-10 D6：可逆操作乐观更新 + 5 秒撤销）。
// 同一时刻只显示一条：新的提示直接替换旧的，旧提示的撤销机会随之结束。
// 鼠标悬停或键盘聚焦在提示条上时暂停计时，避免用户伸手去点“撤销”时它刚好消失。

export type ToastInput = {
  message: string;
  tone?: "neutral" | "danger";
  action?: { label: string; onAction: () => void };
  link?: { label: string; href: string };
  durationMs?: number;
};

type ToastEntry = ToastInput & { id: number };

type ToastContextValue = {
  show: (toast: ToastInput) => void;
  dismiss: () => void;
};

const ToastContext = createContext<ToastContextValue | null>(null);

export const TOAST_DURATION_MS = 5_000;

// motion 的 transition 读不到 CSS 变量，这里复写 --ease-standard 的数值
const EASE_STANDARD: [number, number, number, number] = [0.2, 0.8, 0.2, 1];

export function ToastProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [toast, setToast] = useState<ToastEntry | null>(null);
  const timerRef = useRef<number | null>(null);
  const sequenceRef = useRef(0);
  const reducedMotion = useReducedMotion();

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = null;
  }, []);

  const dismiss = useCallback(() => {
    clearTimer();
    setToast(null);
  }, [clearTimer]);

  const schedule = useCallback(
    (durationMs: number) => {
      clearTimer();
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        setToast(null);
      }, durationMs);
    },
    [clearTimer],
  );

  const show = useCallback(
    (input: ToastInput) => {
      sequenceRef.current += 1;
      setToast({ ...input, id: sequenceRef.current });
      schedule(input.durationMs ?? TOAST_DURATION_MS);
    },
    [schedule],
  );

  useEffect(() => clearTimer, [clearTimer]);

  const value = useMemo(() => ({ show, dismiss }), [show, dismiss]);
  const resume = () => {
    if (toast) schedule(toast.durationMs ?? TOAST_DURATION_MS);
  };
  const action = toast?.action;
  const link = toast?.link;

  return (
    <ToastContext.Provider value={value}>
      {children}
      {/* 播报区常驻 DOM：内容变化才会被读屏可靠地读出 */}
      <div className="toast-region" role="status" aria-live="polite">
        <AnimatePresence>
          {toast && (
            <motion.div
              key={toast.id}
              className="toast"
              data-tone={toast.tone ?? "neutral"}
              initial={reducedMotion ? false : { opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reducedMotion ? { opacity: 0, transition: { duration: 0 } } : { opacity: 0, y: 8 }}
              transition={reducedMotion ? { duration: 0 } : { duration: 0.2, ease: EASE_STANDARD }}
              onPointerEnter={clearTimer}
              onPointerLeave={resume}
              onFocus={clearTimer}
              onBlur={resume}
            >
              {toast.tone === "danger" && <CircleAlert className="toast-icon" aria-hidden="true" />}
              <span className="toast-message">{toast.message}</span>
              {action && (
                <>
                  <span className="toast-separator" aria-hidden="true">·</span>
                  <button
                    className="toast-action"
                    type="button"
                    onClick={() => {
                      dismiss();
                      action.onAction();
                    }}
                  >
                    {action.label}
                  </button>
                </>
              )}
              {link && (
                <>
                  <span className="toast-separator" aria-hidden="true">·</span>
                  <Link className="toast-action" href={link.href} onClick={dismiss}>
                    {link.label}
                  </Link>
                </>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  );
}

const noopToast: ToastContextValue = { show: () => undefined, dismiss: () => undefined };

/** 在 ToastProvider 之外调用时静默降级，组件单独渲染（测试、旧页面）也不会崩。 */
export function useToast(): ToastContextValue {
  return useContext(ToastContext) ?? noopToast;
}
