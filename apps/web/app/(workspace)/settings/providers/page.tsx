import { Settings } from "lucide-react";

import { WorkspaceEmptyState } from "@/components/workspace-empty-state";

export default function ProvidersSettingsPage() {
  return (
    <main className="site-main workspace-page">
      <header className="workspace-page-header">
        <span className="page-kicker">设置</span>
        <h1>服务商</h1>
      </header>
      <WorkspaceEmptyState
        icon={Settings}
        title="暂无服务商配置"
        description="当前账号还没有保存模型或搜索服务配置。"
      />
    </main>
  );
}
