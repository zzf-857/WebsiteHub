"use client";

import { LoaderCircle, RefreshCw } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { useAuth } from "@/components/auth/auth-context";
import { safeNextPath } from "@/lib/auth-contract";

function RouteStatus({
  error,
  onRetry,
}: Readonly<{ error?: string; onRetry?: () => void }>) {
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
          <LoaderCircle className="loading-spinner" aria-hidden="true" />
          <p>正在加载账号...</p>
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
