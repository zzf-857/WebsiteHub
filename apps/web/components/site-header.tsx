"use client";

import {
  Bot,
  Compass,
  Database,
  FileUp,
  Keyboard,
  LogOut,
  MessagesSquare,
  Monitor,
  Moon,
  Search,
  Settings,
  Sun,
} from "lucide-react";
import { motion } from "motion/react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import { useAuth } from "@/components/auth/auth-context";
import { QuickAddButton } from "@/components/discover/quick-add";
import { openShortcutHelp } from "@/components/keyboard/global-shortcuts";
import {
  LIBRARY_SEARCH_LAYOUT_ID,
  LIBRARY_SEARCH_LAYOUT_TRANSITION,
  useLibrarySearchTransition,
} from "@/components/library-search-transition";
import { Spinner } from "@/components/react-bits/spinner";
import type { ThemeMode } from "@/lib/auth-contract";

type HealthState = "checking" | "online" | "offline";

/* 2026-10 方向重定：发现 / · 收藏库 /library · Space /spaces。
   站内对话冻结后挪到账号菜单（/chat），不再占主导航。 */
const navigation = [
  { label: "发现", href: "/", sections: ["/"] },
  { label: "收藏库", href: "/library", sections: ["/library"] },
  { label: "Space", href: "/spaces", sections: ["/spaces"] },
] as const;

/* 账号菜单里的设置入口。/settings/agents 由 Q43 并行建设，这里只负责链接。 */
const accountLinks = [
  { label: "服务商", href: "/settings/providers", icon: Settings },
  { label: "Agent 接入", href: "/settings/agents", icon: Bot },
  { label: "导入书签", href: "/settings/import", icon: FileUp },
  { label: "语义索引", href: "/settings/search-index", icon: Database },
  { label: "站内对话", href: "/chat", icon: MessagesSquare },
] as const;

const themeOptions: ReadonlyArray<{ value: ThemeMode; label: string; icon: typeof Sun }> = [
  { value: "system", label: "跟随系统", icon: Monitor },
  { value: "light", label: "浅色", icon: Sun },
  { value: "dark", label: "深色", icon: Moon },
];

function isNavActive(pathname: string, sections: readonly string[]): boolean {
  return sections.some((section) =>
    section === "/" ? pathname === "/" : pathname === section || pathname.startsWith(`${section}/`),
  );
}

/* 键帽文案：服务端一律渲染设计稿的 ⌘K，客户端水合后按平台修正为 Ctrl K。
   平台信息是外部只读快照，用 useSyncExternalStore 读取可避免 effect 里 setState
   触发的级联渲染，也不会产生水合不一致告警。 */
const subscribeNoop = () => () => {};
const readSearchKeyLabel = () =>
  /mac|iphone|ipad/i.test(window.navigator.userAgent) ? "⌘K" : "Ctrl K";
const readServerSearchKeyLabel = () => "⌘K";

/* SiteHeader 不接受外部 props：导航高亮、搜索转场与账号菜单都由自身状态驱动。 */
export function SiteHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const auth = useAuth();
  const accountRef = useRef<HTMLDetailsElement | null>(null);
  const searchEntryRef = useRef<HTMLButtonElement | null>(null);
  const {
    active: searchTransitionActive,
    start: startSearchTransition,
  } = useLibrarySearchTransition();
  const [health, setHealth] = useState<HealthState>("checking");
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const [themeError, setThemeError] = useState<string | null>(null);
  const isLibrarySection = pathname === "/library" || pathname.startsWith("/library/");
  const searchKeyLabel = useSyncExternalStore(
    subscribeNoop,
    readSearchKeyLabel,
    readServerSearchKeyLabel,
  );

  /* 后端健康轮询保留自旧版顶栏：仅在离线时显示指示，见下方 header-health-dot。 */
  useEffect(() => {
    let active = true;

    const checkHealth = async () => {
      try {
        const response = await fetch("/api/health", { cache: "no-store" });
        const payload = (await response.json()) as { status?: string };
        if (active) setHealth(response.ok && payload.status === "ok" ? "online" : "offline");
      } catch {
        if (active) setHealth("offline");
      }
    };

    void checkHealth();
    const interval = window.setInterval(checkHealth, 30_000);

    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, []);

  const openSearch = useCallback((trigger: "pointer" | "keyboard") => {
    startSearchTransition({
      // 键盘用户已经准备输入，优先立即聚焦；鼠标点击才播放完整的长距离转场。
      animate: trigger === "pointer" && searchEntryRef.current !== null,
      trigger,
    });
  }, [startSearchTransition]);

  /* ⌘K / Ctrl+K 与 “/” 由全站快捷键分发器（components/keyboard/global-shortcuts）统一处理。 */

  /* 路由变化时收起账号浮层，避免跳转后浮层残留。 */
  useEffect(() => {
    accountRef.current?.removeAttribute("open");
  }, [pathname]);

  /* details/summary 原生不支持点击外部或 Escape 关闭，这里补齐浮层的常规预期。 */
  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      const details = accountRef.current;
      if (!details?.open) return;
      if (event.target instanceof Node && details.contains(event.target)) return;
      details.removeAttribute("open");
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      const details = accountRef.current;
      if (event.key !== "Escape" || !details?.open) return;
      event.preventDefault();
      details.removeAttribute("open");
      details.querySelector<HTMLElement>("summary")?.focus();
    };

    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, []);

  const handleLogout = async () => {
    setLoggingOut(true);
    setLogoutError(null);
    try {
      await auth.logout();
      router.replace("/login");
      router.refresh();
    } catch (error) {
      setLogoutError(error instanceof Error ? error.message : "退出失败，请重试");
    } finally {
      setLoggingOut(false);
    }
  };

  const selectTheme = (mode: ThemeMode) => {
    if (mode === auth.theme) return;
    setThemeError(null);
    void auth.setTheme(mode).catch(() => {
      setThemeError("主题设置未保存，请重试");
    });
  };

  const accountLabel = auth.user?.displayName?.trim() || auth.user?.username || "账号";
  /* Array.from 按码点取首字，避免拆开代理对；拉丁字母统一大写。 */
  const avatarChar = (Array.from(accountLabel)[0] ?? "账").toUpperCase();

  return (
    <header className="app-header">
      <Link className="app-brand" href="/" aria-label="WebHub 首页">
        <span className="app-brand-mark" aria-hidden="true">
          <Compass />
        </span>
        <span className="app-brand-name">WebHub</span>
      </Link>

      <nav className="app-nav" aria-label="主导航">
        {navigation.map((item) => {
          const isActive = isNavActive(pathname, item.sections);
          return (
            <Link
              className="app-nav-link"
              data-active={isActive || undefined}
              href={item.href}
              key={item.label}
              aria-current={isActive ? "page" : undefined}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="app-header-spacer" aria-hidden="true" />

      {/* 收藏库页自带搜索框（转场的目标），顶栏入口在那里让位；
          ⌘K/Ctrl+K 全局监听与渲染无关，任何页面都能唤起搜索。 */}
      {isLibrarySection ? null : (
        <motion.button
          ref={searchEntryRef}
          className="app-search-entry"
          type="button"
          layoutId={searchTransitionActive ? LIBRARY_SEARCH_LAYOUT_ID : undefined}
          transition={LIBRARY_SEARCH_LAYOUT_TRANSITION}
          data-transitioning={searchTransitionActive || undefined}
          onClick={(event) => openSearch(event.detail === 0 ? "keyboard" : "pointer")}
          aria-label={`搜索收藏（快捷键 ${searchKeyLabel}）`}
        >
          <Search aria-hidden="true" />
          <span className="app-search-placeholder">搜索收藏</span>
          <kbd className="app-search-kbd" aria-hidden="true">
            {searchKeyLabel}
          </kbd>
        </motion.button>
      )}

      <QuickAddButton />

      {/* 正常状态不该消耗顶栏视觉预算，异常必须可见：
          仅当后端离线时在头像左侧亮出危险色小圆点，在线/检查中完全不渲染。 */}
      {health === "offline" && (
        <span
          className="header-health-dot"
          role="status"
          title="后端服务不可用"
          aria-label="后端服务不可用"
        />
      )}

      <details className="header-account" ref={accountRef}>
        <summary aria-label={`账号菜单：${accountLabel}`}>
          <span className="header-avatar" aria-hidden="true">
            {avatarChar}
          </span>
        </summary>
        <div className="header-account-popover">
          <div className="header-account-identity">
            <strong>{accountLabel}</strong>
            {auth.user?.username && <span>@{auth.user.username}</span>}
          </div>
          {/* 顶栏没有独立设置入口：设置页与冻结的站内对话都收进账号浮层保留可达性。 */}
          {accountLinks.map(({ label, href, icon: Icon }) => (
            <Link
              className="header-account-item"
              href={href}
              key={href}
              aria-current={pathname === href ? "page" : undefined}
            >
              <Icon aria-hidden="true" />
              <span>{label}</span>
            </Link>
          ))}
          <div className="header-account-theme">
            <span className="header-account-theme-label" id="header-theme-label">
              主题
            </span>
            <div className="header-theme-switch" role="group" aria-labelledby="header-theme-label">
              {themeOptions.map(({ value, label, icon: Icon }) => (
                <button
                  key={value}
                  className="header-theme-option"
                  type="button"
                  aria-pressed={auth.theme === value}
                  data-active={auth.theme === value || undefined}
                  aria-label={label}
                  title={label}
                  onClick={() => selectTheme(value)}
                >
                  <Icon aria-hidden="true" />
                </button>
              ))}
            </div>
          </div>
          {themeError && (
            <p className="header-account-error" role="alert">
              {themeError}
            </p>
          )}
          <button
            className="header-account-item"
            type="button"
            onClick={() => {
              accountRef.current?.removeAttribute("open");
              openShortcutHelp();
            }}
          >
            <Keyboard aria-hidden="true" />
            <span>键盘快捷键</span>
            <kbd className="header-account-kbd" aria-hidden="true">?</kbd>
          </button>
          <div className="header-account-divider" aria-hidden="true" />
          <button
            className="header-account-item"
            type="button"
            onClick={() => void handleLogout()}
            disabled={loggingOut}
          >
            {/* 加载态统一走 ReactBits Spinner（纯 CSS 圆环，内部已处理 reduced-motion 降级） */}
            {loggingOut ? <Spinner size={16} /> : <LogOut aria-hidden="true" />}
            <span>{loggingOut ? "正在退出" : "退出登录"}</span>
          </button>
          {logoutError && (
            <p className="header-account-error" role="alert">
              {logoutError}
            </p>
          )}
        </div>
      </details>
    </header>
  );
}
