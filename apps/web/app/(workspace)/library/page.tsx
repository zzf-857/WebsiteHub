import { Library } from "lucide-react";

import { WorkspaceEmptyState } from "@/components/workspace-empty-state";

export default function LibraryPage() {
  return (
    <main className="site-main workspace-page">
      <header className="workspace-page-header">
        <span className="page-kicker">WebHub</span>
        <h1>资料库</h1>
      </header>
      <WorkspaceEmptyState
        icon={Library}
        title="暂无收藏"
        description="当前账号还没有保存网站。"
      />
    </main>
  );
}
