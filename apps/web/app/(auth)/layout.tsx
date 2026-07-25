import type { ReactNode } from "react";
import Link from "next/link";

import { GuestGate } from "@/components/auth/auth-gate";
import { ThemeToggle } from "@/components/theme-toggle";

export default function AuthLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <div className="auth-page">
      <header className="auth-header">
        <Link className="brand" href="/login" aria-label="WebHub 登录页">
          <span className="brand-mark" aria-hidden="true">W</span>
          <span className="brand-name">WebHub</span>
        </Link>
        <ThemeToggle />
      </header>
      <GuestGate>{children}</GuestGate>
      <footer className="auth-footer">WebHub LAN</footer>
    </div>
  );
}
