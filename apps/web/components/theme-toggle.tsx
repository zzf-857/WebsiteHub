"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { useState } from "react";

import { useAuth } from "@/components/auth/auth-context";
import type { ThemeMode } from "@/lib/auth-contract";

const themeOrder: ThemeMode[] = ["system", "light", "dark"];
const themeLabels: Record<ThemeMode, string> = {
  system: "跟随系统",
  light: "浅色主题",
  dark: "深色主题",
};

function ThemeIcon({ mode }: Readonly<{ mode: ThemeMode }>) {
  if (mode === "light") return <Sun aria-hidden="true" />;
  if (mode === "dark") return <Moon aria-hidden="true" />;
  return <Monitor aria-hidden="true" />;
}

/* className 可由使用方覆盖：顶栏传入 header.css 里的 header-icon-btn 以对齐设计稿，
   登录页等旧场景缺省仍走 globals 里的 icon-button，互不影响。 */
export function ThemeToggle({ className = "icon-button" }: Readonly<{ className?: string }>) {
  const auth = useAuth();
  const [error, setError] = useState<string | null>(null);

  const cycleTheme = () => {
    setError(null);
    const nextTheme = themeOrder[(themeOrder.indexOf(auth.theme) + 1) % themeOrder.length];
    void auth.setTheme(nextTheme).catch(() => {
      setError("主题设置未保存，请重试");
    });
  };

  return (
    <div className="theme-control">
      <button
        className={className}
        type="button"
        onClick={cycleTheme}
        aria-label={`当前为${themeLabels[auth.theme]}，点击切换主题`}
        aria-describedby={error ? "theme-error" : undefined}
        title={themeLabels[auth.theme]}
      >
        <ThemeIcon mode={auth.theme} />
      </button>
      {error && <span className="theme-error" id="theme-error" role="alert">{error}</span>}
    </div>
  );
}
