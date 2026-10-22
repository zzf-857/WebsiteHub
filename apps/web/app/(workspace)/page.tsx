import type { Metadata } from "next";

import { DiscoverPage } from "@/components/discover/discover-page";

export const metadata: Metadata = { title: "WebHub · 发现" };

// 放在 (workspace) route group 下：group 不占 URL 段，"/" 仍是首页，
// 同时自动套上 (workspace)/layout.tsx 的 AuthGate + SiteHeader，无需重复渲染。
export default function WorkspaceHomePage() {
  return <DiscoverPage />;
}
