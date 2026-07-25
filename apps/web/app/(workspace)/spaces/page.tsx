import { Blocks } from "lucide-react";

import { WorkspaceEmptyState } from "@/components/workspace-empty-state";

export default function SpacesPage() {
  return (
    <main className="site-main workspace-page">
      <header className="workspace-page-header">
        <span className="page-kicker">WebHub</span>
        <h1>Space</h1>
      </header>
      <WorkspaceEmptyState
        icon={Blocks}
        title="暂无 Space"
        description="当前账号还没有创建 Space。"
      />
    </main>
  );
}
