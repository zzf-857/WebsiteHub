import { SemanticIndexPanel } from "@/components/settings/semantic-index-panel";

export default function SemanticIndexSettingsPage() {
  return (
    <main className="site-main">
      <header className="workspace-page-header">
        <span className="page-kicker">设置</span>
        <h1>语义索引</h1>
        <p className="provider-page-lead">
          管理网址库的向量索引与重建任务。索引只用于增强相关度排序，不会改变已保存的网站。
        </p>
      </header>

      <div className="provider-page">
        <SemanticIndexPanel />
      </div>
    </main>
  );
}
