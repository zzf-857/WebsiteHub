import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { AuthProvider } from "@/components/auth/auth-context";

import "./globals.css";

const initialThemeScript = `(() => {
  try {
    const theme = window.localStorage.getItem("webhub-theme");
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else document.documentElement.removeAttribute("data-theme");
  } catch {}
})();`;

export const metadata: Metadata = {
  title: "WebHub",
  description: "Agent-first personal website knowledge hub",
};

export const viewport: Viewport = {
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f9f9fe" },
    { media: "(prefers-color-scheme: dark)", color: "#111214" },
  ],
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN" data-scroll-behavior="smooth" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: initialThemeScript }} />
      </head>
      <body>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
