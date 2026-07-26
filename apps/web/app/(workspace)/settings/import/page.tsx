import { BookmarkImportWorkspace } from "@/components/settings/bookmark-import-workspace";

export default function BookmarkImportPage() {
  return (
    <main className="site-main">
      <header className="workspace-page-header">
        <span className="page-kicker">设置</span>
        <h1>导入书签</h1>
        <p className="provider-page-lead">
          把浏览器导出的书签一次性收进资料库。上传后先给你一份统计，确认之前资料库不会有任何变化。
        </p>
      </header>
      <BookmarkImportWorkspace />
    </main>
  );
}
