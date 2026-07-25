"use client";

import {
  Blocks,
  Bot,
  ChevronDown,
  CircleUserRound,
  Library,
  LoaderCircle,
  LogOut,
  Settings,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth/auth-context";
import { ThemeToggle } from "@/components/theme-toggle";

type HealthState = "checking" | "online" | "offline";

const navigation = [
  { label: "Agent", icon: Bot, href: "/chat/new", section: "/chat" },
  { label: "资料库", icon: Library, href: "/library", section: "/library" },
  { label: "Space", icon: Blocks, href: "/spaces", section: "/spaces" },
] as const;

const healthLabels: Record<HealthState, string> = {
  checking: "正在连接后端",
  online: "后端已连接",
  offline: "后端不可用",
};

export function SiteHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const auth = useAuth();
  const [health, setHealth] = useState<HealthState>("checking");
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);

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

  const accountLabel = auth.user?.displayName ?? auth.user?.username ?? "账号";

  return (
    <header className="site-header">
      <div className="site-header-inner">
        <Link className="brand" href="/chat/new" aria-label="WebHub 首页">
          <span className="brand-mark" aria-hidden="true">W</span>
          <span className="brand-name">WebHub</span>
        </Link>

        <nav className="site-navigation" aria-label="主导航">
          {navigation.map((item) => {
            const Icon = item.icon;
            const isActive = pathname === item.section || pathname.startsWith(`${item.section}/`);

            return (
              <Link
                className="site-nav-link"
                data-active={isActive || undefined}
                href={item.href}
                key={item.label}
                aria-current={isActive ? "page" : undefined}
              >
                <Icon aria-hidden="true" />
                <span>{item.label}</span>
              </Link>
            );
          })}
        </nav>

        <div className="site-actions">
          <div
            className="health-status"
            data-state={health}
            title={healthLabels[health]}
            aria-live="polite"
          >
            <span className="health-dot" aria-hidden="true" />
            <span>{healthLabels[health]}</span>
          </div>
          <ThemeToggle />
          <Link className="icon-button" href="/settings/providers" aria-label="设置" title="设置">
            <Settings aria-hidden="true" />
          </Link>
          <details className="account-menu">
            <summary className="account-menu-trigger" aria-label={`账号菜单：${accountLabel}`}>
              <CircleUserRound aria-hidden="true" />
              <span className="account-name">{accountLabel}</span>
              <ChevronDown className="account-chevron" aria-hidden="true" />
            </summary>
            <div className="account-popover">
              <div className="account-identity">
                <strong>{accountLabel}</strong>
                <span>@{auth.user?.username}</span>
              </div>
              <button type="button" onClick={() => void handleLogout()} disabled={loggingOut}>
                {loggingOut ? (
                  <LoaderCircle className="loading-spinner" aria-hidden="true" />
                ) : (
                  <LogOut aria-hidden="true" />
                )}
                {loggingOut ? "正在退出" : "退出登录"}
              </button>
              {logoutError && <p role="alert">{logoutError}</p>}
            </div>
          </details>
        </div>
      </div>
    </header>
  );
}
