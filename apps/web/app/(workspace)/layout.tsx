import type { ReactNode } from "react";

import { AuthGate } from "@/components/auth/auth-gate";
import { LibrarySearchTransitionProvider } from "@/components/library-search-transition";
import { SiteHeader } from "@/components/site-header";
import { ToastProvider } from "@/components/ui/toast";

export default function WorkspaceLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <AuthGate>
      <LibrarySearchTransitionProvider>
        {/* 底部提示条全站共用：顶栏快速收藏与发现页的撤销提示走同一个出口 */}
        <ToastProvider>
          <div className="site-shell">
            <SiteHeader />
            {children}
          </div>
        </ToastProvider>
      </LibrarySearchTransitionProvider>
    </AuthGate>
  );
}
