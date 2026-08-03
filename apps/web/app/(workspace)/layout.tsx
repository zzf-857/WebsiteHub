import type { ReactNode } from "react";

import { AuthGate } from "@/components/auth/auth-gate";
import { LibrarySearchTransitionProvider } from "@/components/library-search-transition";
import { SiteHeader } from "@/components/site-header";

export default function WorkspaceLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <AuthGate>
      <LibrarySearchTransitionProvider>
        <div className="site-shell">
          <SiteHeader />
          {children}
        </div>
      </LibrarySearchTransitionProvider>
    </AuthGate>
  );
}
