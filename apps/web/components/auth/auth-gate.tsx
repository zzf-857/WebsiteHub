"use client";

import { RefreshCw } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { useAuth } from "@/components/auth/auth-context";
import { Spinner } from "@/components/react-bits/spinner";
import { safeNextPath } from "@/lib/auth-contract";

function RouteStatus({
  error,
  message = "正在加载账号...",
  onRetry,
}: Readonly<{ error?: string; message?: string; onRetry?: () => void }>) {
  return (
    <main className="route-status" aria-live="polite">
      <span className="route-status-mark" aria-hidden="true">W</span>
      {error ? (
        <>
          <h1>无法验证登录状态</h1>
          <p>{error}</p>
          <button type="button" onClick={onRetry}>
            <RefreshCw aria-hidden="true" />
            重试
          </button>
        </>
      ) : (
        <>
          <Spinner />
          <p>{message}</p>
        </>
      )}
    </main>
  );
}

export function AuthGate({ children }: Readonly<{ children: ReactNode }>) {
  const auth = useAuth();
  const pathname = usePathname();
  const router = useRouter();

  useEffect(() => {
    if (auth.status === "anonymous") {
      const nextPath = `${pathname}${window.location.search}`;
      router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
    }
  }, [auth.status, pathname, router]);

  if (auth.status === "authenticated") return children;
  if (auth.status === "error") {
    return (
      <RouteStatus
        error={auth.error}
        onRetry={() => { void auth.refresh().catch(() => undefined); }}
      />
    );
  }
  if (auth.status === "anonymous") {
    return <RouteStatus message="未登录，正在跳转到登录页..." />;
  }
  return <RouteStatus />;
}

export function GuestGate({ children }: Readonly<{ children: ReactNode }>) {
  const auth = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (auth.status === "authenticated") {
      const nextPath = safeNextPath(new URLSearchParams(window.location.search).get("next"));
      router.replace(nextPath);
    }
  }, [auth.status, router]);

  if (auth.status === "anonymous") return children;
  if (auth.status === "error") {
    return (
      <RouteStatus
        error={auth.error}
        onRetry={() => { void auth.refresh().catch(() => undefined); }}
      />
    );
  }
  return <RouteStatus />;
}
