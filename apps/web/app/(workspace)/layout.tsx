import type { ReactNode } from "react";

import { AuthGate } from "@/components/auth/auth-gate";
import { SiteHeader } from "@/components/site-header";

export default function WorkspaceLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <AuthGate>
      <div className="site-shell">
        <SiteHeader />
        {children}
      </div>
    </AuthGate>
  );
}
