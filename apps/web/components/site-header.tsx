"use client";

import { Compass, FileUp, LogOut, Search, Settings, Sparkles } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import { useAuth } from "@/components/auth/auth-context";
import { Spinner } from "@/components/react-bits/spinner";
import { ThemeToggle } from "@/components/theme-toggle";

type HealthState = "checking" | "online" | "offline";

/* 路由映射按设计稿 1a：首页 / · 网址库 /library · Space /spaces。
   Agent 对话挂在首页的 Agent 模块下，因此 /chat 路径也算作"首页"高亮。 */
const navigation = [
  { label: "首页", href: "/", sections: ["/", "/chat"] },
  { label: "网址库", href: "/library", sections: ["/library"] },
  { label: "Space", href: "/spaces", sections: ["/spaces"] },
] as const;

function isNavActive(pathname: string, sections: readonly string[]): boolean {
  return sections.some((section) =>
    section === "/" ? pathname === "/" : pathname === section || pathname.startsWith(`${section}/`),
  );
}

/* 判断当前按键事件是否发生在输入场景（输入框 / 文本域 / 可编辑区域）。
   此时不抢占 ⌘K/Ctrl+K，避免破坏浏览器或页面内已有的输入快捷键。 */
function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

/* 键帽文案：服务端一律渲染设计稿的 ⌘K，客户端水合后按平台修正为 Ctrl K。
   平台信息是外部只读快照，用 useSyncExternalStore 读取可避免 effect 里 setState
   触发的级联渲染，也不会产生水合不一致告警。 */
const subscribeNoop = () => () => {};
const readSearchKeyLabel = () =>
  /mac|iphone|ipad/i.test(window.navigator.userAgent) ? "⌘K" : "Ctrl K";
const readServerSearchKeyLabel = () => "⌘K";

/* 本轮契约：SiteHeader 不接受外部 props，吸顶态由自身观察 #agent-panel 驱动。 */
export function SiteHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const auth = useAuth();
  const accountRef = useRef<HTMLDetailsElement | null>(null);
  const [health, setHealth] = useState<HealthState>("checking");
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  /* 设计稿 1c：首页滚过 Agent 模块后 Header 收紧（64px → 56px），
     搜索框让位给「回到 Agent」。仅首页启用，其他路由永远保持常规形态。 */
  const isHome = pathname === "/";
  const [compact, setCompact] = useState(false);
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

  /* 吸顶态观察：#agent-panel 完全滚出视口 → data-compact，滚回来 → 复原。
     首页主体是客户端组件，Agent 模块可能晚于 Header 挂载；这里选 MutationObserver
     而非 requestAnimationFrame 轮询：无需猜测重试次数与时长，元素一出现即挂上
     IntersectionObserver 并立刻停止 DOM 监听，空转成本更低。 */
  useEffect(() => {
    if (!isHome) return;

    let intersection: IntersectionObserver | null = null;
    let mutation: MutationObserver | null = null;

    const observePanel = (panel: Element) => {
      intersection = new IntersectionObserver(([entry]) => {
        setCompact(!entry.isIntersecting);
      });
      intersection.observe(panel);
    };

    const panel = document.getElementById("agent-panel");
    if (panel) {
      observePanel(panel);
    } else {
      mutation = new MutationObserver(() => {
        const late = document.getElementById("agent-panel");
        if (!late) return;
        mutation?.disconnect();
        mutation = null;
        observePanel(late);
      });
      mutation.observe(document.body, { childList: true, subtree: true });
    }

    return () => {
      intersection?.disconnect();
      mutation?.disconnect();
      /* 清理阶段复位，确保带着紧凑态离开首页时 data-compact 不泄漏到其他路由 */
      setCompact(false);
    };
  }, [isHome]);

  /* 「回到 Agent」：滚回模块顶部；系统减弱动效时不做平滑滚动，瞬时跳转。 */
  const handleBackToAgent = useCallback(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    document.getElementById("agent-panel")?.scrollIntoView({
      block: "start",
      behavior: reduced ? "auto" : "smooth",
    });
  }, []);

  const openSearch = useCallback(() => {
    router.push("/library?focus=search");
  }, [router]);

  /* 全局 ⌘K/Ctrl+K 唤起搜索；输入场景不抢占，卸载时清理监听。 */
  useEffect(() => {
    const handleKeydown = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "k") return;
      if (isTypingTarget(event.target)) return;
      event.preventDefault();
      openSearch();
    };

    window.addEventListener("keydown", handleKeydown);
    return () => window.removeEventListener("keydown", handleKeydown);
  }, [openSearch]);

  /* 路由变化时收起账号浮层，避免跳转后浮层残留。 */
  useEffect(() => {
    accountRef.current?.removeAttribute("open");
  }, [pathname]);

  /* details/summary 原生不支持点击外部关闭，这里补上以符合浮层的常规预期。 */
  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      const details = accountRef.current;
      if (!details?.open) return;
      if (event.target instanceof Node && details.contains(event.target)) return;
      details.removeAttribute("open");
    };

    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
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

  const accountLabel = auth.user?.displayName?.trim() || auth.user?.username || "账号";
  /* Array.from 按码点取首字，避免拆开代理对；拉丁字母统一大写。 */
  const avatarChar = (Array.from(accountLabel)[0] ?? "账").toUpperCase();

  return (
    <header className="app-header" data-compact={compact || undefined}>
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

      {/* 紧凑态下搜索框被「回到 Agent」取代，但上面的 ⌘K/Ctrl+K 全局监听
          与渲染无关，快捷键依旧能唤起搜索，能力不因吸顶而消失。 */}
      {compact ? (
        <button className="app-back-to-agent" type="button" onClick={handleBackToAgent}>
          <Sparkles aria-hidden="true" />
          <span>回到 Agent</span>
        </button>
      ) : (
        <button
          className="app-search-entry"
          type="button"
          onClick={openSearch}
          aria-label={`精确搜索站内网址（快捷键 ${searchKeyLabel}）`}
        >
          <Search aria-hidden="true" />
          <span className="app-search-placeholder">精确搜索站内网址</span>
          <kbd className="app-search-kbd" aria-hidden="true">
            {searchKeyLabel}
          </kbd>
        </button>
      )}

      <ThemeToggle className="header-icon-btn" />

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
          {/* 设计稿顶栏没有独立设置入口，原有的设置页链接收进账号浮层保留可达性。 */}
          <Link className="header-account-item" href="/settings/providers">
            <Settings aria-hidden="true" />
            <span>模型服务设置</span>
          </Link>
          <Link className="header-account-item" href="/settings/import">
            <FileUp aria-hidden="true" />
            <span>导入书签</span>
          </Link>
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
