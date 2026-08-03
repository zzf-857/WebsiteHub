"use client";

import { LayoutGroup, MotionConfig, useReducedMotion } from "motion/react";
import type { Transition } from "motion/react";
import { usePathname, useRouter } from "next/navigation";
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

export const LIBRARY_SEARCH_LAYOUT_ID = "library-search-shared-element";
export const LIBRARY_LAYOUT_STORAGE_KEY = "webhub:library-layout-mode";

export const LIBRARY_SEARCH_LAYOUT_TRANSITION: Transition = {
  type: "tween",
  duration: 0.34,
  ease: [0.22, 1, 0.36, 1] as [number, number, number, number],
};

type LibrarySearchTrigger = "pointer" | "keyboard";

type StartLibrarySearchOptions = {
  animate: boolean;
  trigger: LibrarySearchTrigger;
};

type LibrarySearchTransitionValue = {
  active: boolean;
  trigger: LibrarySearchTrigger | null;
  start: (options: StartLibrarySearchOptions) => void;
  finish: () => void;
  registerTarget: (node: HTMLElement | null) => void;
  focusTarget: () => boolean;
};

const LibrarySearchTransitionContext = createContext<LibrarySearchTransitionValue | null>(null);

const TRANSITION_TIMEOUT_MS = 680;
const ROOT_LIBRARY_PATH = "/library";
const ROOT_LIBRARY_SEARCH_PATH = "/library?focus=search";

function setDocumentTransitionState(trigger: LibrarySearchTrigger) {
  document.documentElement.dataset.librarySearchTransition = "active";
  document.documentElement.dataset.librarySearchTransitionTrigger = trigger;
  try {
    const layout = window.localStorage.getItem(LIBRARY_LAYOUT_STORAGE_KEY);
    if (layout === "centered" || layout === "full") {
      document.documentElement.dataset.librarySearchLayout = layout;
    }
  } catch {
    // 存储不可用时沿用页面默认宽度，不能让动效阻断导航。
  }
}

function clearDocumentTransitionState() {
  delete document.documentElement.dataset.librarySearchTransition;
  delete document.documentElement.dataset.librarySearchTransitionTrigger;
  delete document.documentElement.dataset.librarySearchLayout;
}

export function LibrarySearchTransitionProvider({ children }: Readonly<{ children: ReactNode }>) {
  const pathname = usePathname();
  const router = useRouter();
  const reducedMotion = useReducedMotion();
  const [active, setActive] = useState(false);
  const [trigger, setTrigger] = useState<LibrarySearchTrigger | null>(null);
  const activeRef = useRef(false);
  const lockedRef = useRef(false);
  const pendingFocusRef = useRef(false);
  const targetRef = useRef<HTMLElement | null>(null);
  const navigationFrameRef = useRef<number | null>(null);
  const timeoutRef = useRef<number | null>(null);

  const cancelScheduledWork = useCallback(() => {
    if (navigationFrameRef.current !== null) {
      window.cancelAnimationFrame(navigationFrameRef.current);
      navigationFrameRef.current = null;
    }
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }, []);

  const focusTarget = useCallback(() => {
    const target = targetRef.current;
    if (!target?.isConnected) return false;

    target.focus({ preventScroll: true });
    const focused = document.activeElement === target;
    if (focused) pendingFocusRef.current = false;
    return focused;
  }, []);

  const finish = useCallback(() => {
    cancelScheduledWork();
    activeRef.current = false;
    lockedRef.current = false;
    setActive(false);
    setTrigger(null);
    clearDocumentTransitionState();
    focusTarget();
  }, [cancelScheduledWork, focusTarget]);

  const handleTimeout = useCallback(() => {
    timeoutRef.current = null;
    // 后台标签页会暂停 rAF；超时收口时必须先兑现导航，不能把唯一一次 push 一并取消。
    if (navigationFrameRef.current !== null) {
      window.cancelAnimationFrame(navigationFrameRef.current);
      navigationFrameRef.current = null;
      router.push(ROOT_LIBRARY_SEARCH_PATH);
    }
    finish();
  }, [finish, router]);

  const armTimeout = useCallback(() => {
    if (timeoutRef.current !== null) window.clearTimeout(timeoutRef.current);
    timeoutRef.current = window.setTimeout(handleTimeout, TRANSITION_TIMEOUT_MS);
  }, [handleTimeout]);

  const navigateDirectly = useCallback(() => {
    pendingFocusRef.current = true;
    lockedRef.current = true;
    armTimeout();
    router.push(ROOT_LIBRARY_SEARCH_PATH);
  }, [armTimeout, router]);

  const start = useCallback(
    (options: StartLibrarySearchOptions) => {
      if (lockedRef.current) return;

      const target = targetRef.current;
      if (pathname === ROOT_LIBRARY_PATH) {
        pendingFocusRef.current = true;
        if (target?.isConnected) {
          focusTarget();
          return;
        }
        navigateDirectly();
        return;
      }

      if (pathname.startsWith(`${ROOT_LIBRARY_PATH}/`)) {
        if (target?.isConnected) {
          pendingFocusRef.current = true;
          focusTarget();
        } else {
          navigateDirectly();
        }
        return;
      }

      if (reducedMotion || !options.animate) {
        navigateDirectly();
        return;
      }

      lockedRef.current = true;
      pendingFocusRef.current = true;
      activeRef.current = true;
      setTrigger(options.trigger);
      setActive(true);
      setDocumentTransitionState(options.trigger);
      armTimeout();

      // Give the shared source element one committed frame before replacing the route tree.
      navigationFrameRef.current = window.requestAnimationFrame(() => {
        navigationFrameRef.current = null;
        router.push(ROOT_LIBRARY_SEARCH_PATH);
      });
    }, [armTimeout, focusTarget, navigateDirectly, pathname, reducedMotion, router],
  );

  const registerTarget = useCallback(
    (node: HTMLElement | null) => {
      targetRef.current = node;
      if (!node || !pendingFocusRef.current || activeRef.current) return;

      window.requestAnimationFrame(() => {
        if (targetRef.current !== node || !pendingFocusRef.current || activeRef.current) return;
        finish();
      });
    },
    [finish],
  );

  useEffect(() => {
    router.prefetch(ROOT_LIBRARY_PATH);
  }, [router]);

  useEffect(() => {
    if (pathname === ROOT_LIBRARY_PATH && pendingFocusRef.current && !activeRef.current) {
      focusTarget();
    }
  }, [focusTarget, pathname]);

  useEffect(
    () => () => {
      cancelScheduledWork();
      clearDocumentTransitionState();
    },
    [cancelScheduledWork],
  );

  const value = useMemo<LibrarySearchTransitionValue>(
    () => ({ active, trigger, start, finish, registerTarget, focusTarget }),
    [active, finish, focusTarget, registerTarget, start, trigger],
  );

  return (
    <LibrarySearchTransitionContext.Provider value={value}>
      <MotionConfig reducedMotion="user" transition={LIBRARY_SEARCH_LAYOUT_TRANSITION}>
        <LayoutGroup id="library-search-transition">{children}</LayoutGroup>
      </MotionConfig>
    </LibrarySearchTransitionContext.Provider>
  );
}

export function useLibrarySearchTransition(): LibrarySearchTransitionValue {
  const value = useContext(LibrarySearchTransitionContext);
  if (!value) {
    throw new Error(
      "useLibrarySearchTransition must be used within LibrarySearchTransitionProvider",
    );
  }
  return value;
}
