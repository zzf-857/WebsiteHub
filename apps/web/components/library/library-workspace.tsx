"use client";

import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  FolderCog,
  FolderTree,
  LayoutGrid,
  List,
  LoaderCircle,
  Pin,
  Plus,
  RefreshCw,
  Search,
  Tags,
  Trash2,
  WandSparkles,
} from "lucide-react";
import {
  LibraryDialog,
} from "@/components/library/library-dialog";
import {
  SiteForm,
} from "@/components/library/site-form";
import {
  TaxonomyManager,
} from "@/components/library/taxonomy-manager";
import {
  SiteFavicon,
} from "@/components/site-favicon";
import {
  SiteCollection,
  siteHost,
} from "@/components/library/library-workspace-parts";
import { useLibraryWorkspace } from "@/components/library/use-library-workspace";

export function LibraryWorkspace() {
  const {
    analysisBackfillBusy,
    categories,
    categoryId,
    closeDialog,
    collectionProps,
    dialog,
    direction,
    handleCreate,
    handleAnalysisBackfill,
    handleDelete,
    handleSortChange,
    handleTaxonomyChanged,
    handleUpdate,
    hasActiveFilters,
    hasSites,
    loadMore,
    loadTaxonomies,
    mutationBusy,
    mutationError,
    notice,
    openDialog,
    pinnedOnly,
    pinnedPage,
    refreshSites,
    regularPage,
    searchInput,
    searchInputRef,
    setCategoryId,
    setDirection,
    setNotice,
    setPinnedOnly,
    setSearchInput,
    setSearchQuery,
    setTagId,
    setViewMode,
    sitesError,
    sitesLoading,
    sort,
    tagId,
    tags,
    taxonomyError,
    taxonomyLoading,
    totalLibrarySites,
    totalMatched,
    viewMode,
  } = useLibraryWorkspace();

  return (
    <main className="site-main library-workspace">
      <header className="workspace-page-header library-page-header">
        <div>
          <span className="page-kicker">WebHub</span>
          <h1>资料库</h1>
          <p>整理、检索并维护当前账号保存的网站。</p>
        </div>
        <div className="library-page-actions">
          <button
            className="library-button secondary"
            type="button"
            onClick={() => void handleAnalysisBackfill()}
            disabled={analysisBackfillBusy || totalLibrarySites === 0}
            title="补全未分析网站的公开信息"
          >
            {analysisBackfillBusy ? (
              <LoaderCircle className="loading-spinner" aria-hidden="true" />
            ) : (
              <WandSparkles aria-hidden="true" />
            )}
            {analysisBackfillBusy ? "正在启动" : "补全网站信息"}
          </button>
          <button
            className="library-button secondary"
            type="button"
            onClick={() => openDialog({ kind: "taxonomy" })}
          >
            <FolderCog aria-hidden="true" />
            管理分类与标签
          </button>
          <button className="library-button primary" type="button" onClick={() => openDialog({ kind: "create" })}>
            <Plus aria-hidden="true" />
            新增网站
          </button>
        </div>
      </header>

      <div className="library-layout">
        <aside className="library-sidebar" aria-label="资料库分类">
          <section className="library-sidebar-section">
            <h2>浏览</h2>
            <button
              className="library-sidebar-item"
              type="button"
              data-active={!pinnedOnly && !categoryId || undefined}
              onClick={() => { setPinnedOnly(false); setCategoryId(""); }}
            >
              <span><LayoutGrid aria-hidden="true" />全部网站</span>
              <small>{totalLibrarySites}</small>
            </button>
            <button
              className="library-sidebar-item"
              type="button"
              data-active={pinnedOnly || undefined}
              onClick={() => setPinnedOnly(true)}
            >
              <span><Pin aria-hidden="true" />置顶网站</span>
              <small>{pinnedPage.matchedCount}</small>
            </button>
          </section>

          <section className="library-sidebar-section">
            <div className="library-sidebar-heading">
              <h2>分类</h2>
              {taxonomyLoading && <LoaderCircle className="loading-spinner" aria-label="正在加载分类" />}
            </div>
            <div className="library-category-list">
              {categories.map((category) => (
                <button
                  className="library-sidebar-item"
                  type="button"
                  key={category.id}
                  data-active={categoryId === category.id || undefined}
                  onClick={() => setCategoryId((current) => current === category.id ? "" : category.id)}
                >
                  <span><FolderTree aria-hidden="true" />{category.name}</span>
                  <small>{category.siteCount}</small>
                </button>
              ))}
              {!taxonomyLoading && categories.length === 0 && (
                <p className="library-inline-empty">暂无分类</p>
              )}
            </div>
          </section>
        </aside>

        <div className="library-content">
          <div className="library-toolbar">
            <label className="library-search-field">
              <Search aria-hidden="true" />
              <span className="sr-only">搜索资料库</span>
              <input
                ref={searchInputRef}
                type="search"
                placeholder="搜索名称、网址或描述"
                value={searchInput}
                onChange={(event) => setSearchInput(event.target.value)}
              />
            </label>
            <label className="library-filter-select">
              <Tags aria-hidden="true" />
              <span className="sr-only">按标签筛选</span>
              <select value={tagId} onChange={(event) => setTagId(event.target.value)} disabled={taxonomyLoading}>
                <option value="">全部标签</option>
                {tags.map((tag) => (
                  <option key={tag.id} value={tag.id}>{tag.name} ({tag.siteCount})</option>
                ))}
              </select>
            </label>
            <label className="library-filter-select">
              <span className="sr-only">排序字段</span>
              <select value={sort} onChange={handleSortChange}>
                <option value="updated">最近更新</option>
                <option value="created">创建时间</option>
                <option value="name">网站名称</option>
                <option value="custom">自定义顺序</option>
                {/* 没有搜索词时后端会 422，所以直接禁用而不是让用户选了才报错 */}
                <option value="relevance" disabled={!searchInput.trim()}>
                  相关度{searchInput.trim() ? "" : "（需先输入搜索词）"}
                </option>
              </select>
            </label>
            <button
              className="icon-button library-direction-button"
              type="button"
              onClick={() => setDirection((current) => current === "asc" ? "desc" : "asc")}
              aria-label={direction === "asc" ? "当前升序，切换为降序" : "当前降序，切换为升序"}
              title={direction === "asc" ? "升序" : "降序"}
            >
              {direction === "asc" ? <ArrowUp aria-hidden="true" /> : <ArrowDown aria-hidden="true" />}
            </button>
            <div className="library-view-toggle" role="group" aria-label="视图模式">
              <button
                className="icon-button"
                type="button"
                data-active={viewMode === "grid" || undefined}
                onClick={() => setViewMode("grid")}
                aria-label="网格视图"
                title="网格视图"
              >
                <LayoutGrid aria-hidden="true" />
              </button>
              <button
                className="icon-button"
                type="button"
                data-active={viewMode === "list" || undefined}
                onClick={() => setViewMode("list")}
                aria-label="列表视图"
                title="列表视图"
              >
                <List aria-hidden="true" />
              </button>
            </div>
          </div>

          <div className="library-results-heading" aria-live="polite">
            <span>{sitesLoading ? "正在读取资料库" : `共 ${totalMatched} 个结果`}</span>
            {hasActiveFilters && (
              <button
                type="button"
                onClick={() => {
                  setSearchInput("");
                  setSearchQuery("");
                  setCategoryId("");
                  setTagId("");
                  setPinnedOnly(false);
                }}
              >
                清除筛选
              </button>
            )}
          </div>

          {notice && (
            <div className="library-notice" role="status">
              <span>{notice}</span>
              <button type="button" onClick={() => setNotice(null)}>关闭</button>
            </div>
          )}

          {taxonomyError && (
            <div className="library-error-banner" role="alert">
              <AlertCircle aria-hidden="true" />
              <span>{taxonomyError}</span>
              <button type="button" onClick={() => void loadTaxonomies().catch(() => undefined)}>
                <RefreshCw aria-hidden="true" />重试
              </button>
            </div>
          )}

          {sitesError && (
            <div className="library-error-banner" role="alert">
              <AlertCircle aria-hidden="true" />
              <span>{sitesError}</span>
              <button type="button" onClick={refreshSites}>
                <RefreshCw aria-hidden="true" />重试
              </button>
            </div>
          )}

          {sitesLoading && !hasSites ? (
            <div className="library-loading-grid" aria-label="正在加载网站">
              {Array.from({ length: 6 }, (_, index) => (
                <div className="library-site-skeleton" key={index} aria-hidden="true" />
              ))}
            </div>
          ) : !hasSites && !sitesError ? (
            <section className="library-empty-state">
              <span className="library-empty-icon" aria-hidden="true"><Search /></span>
              <h2>{hasActiveFilters ? "没有匹配的网站" : "资料库还是空的"}</h2>
              <p>{hasActiveFilters ? "调整关键词或筛选条件后再试。" : "新增第一个网站，开始建立个人网站知识库。"}</p>
              {hasActiveFilters ? (
                <button
                  className="library-button secondary"
                  type="button"
                  onClick={() => {
                    setSearchInput("");
                    setSearchQuery("");
                    setCategoryId("");
                    setTagId("");
                    setPinnedOnly(false);
                  }}
                >
                  清除筛选
                </button>
              ) : (
                <button className="library-button primary" type="button" onClick={() => openDialog({ kind: "create" })}>
                  <Plus aria-hidden="true" />新增网站
                </button>
              )}
            </section>
          ) : (
            <div className="library-sections">
              {pinnedPage.items.length > 0 && (
                <section className="library-pinned-section">
                  <div className="library-section-heading">
                    <div>
                      <Pin aria-hidden="true" />
                      <h2>置顶网站</h2>
                    </div>
                    <span>{pinnedPage.matchedCount} 个</span>
                  </div>
                  <SiteCollection sites={pinnedPage.items} {...collectionProps} />
                  {pinnedPage.nextCursor && (
                    <button
                      className="library-load-more"
                      type="button"
                      onClick={() => void loadMore("pinned")}
                      disabled={pinnedPage.loadingMore}
                    >
                      {pinnedPage.loadingMore && <LoaderCircle className="loading-spinner" aria-hidden="true" />}
                      {pinnedPage.loadingMore ? "正在加载" : "加载更多置顶网站"}
                    </button>
                  )}
                </section>
              )}

              {!pinnedOnly && regularPage.items.length > 0 && (
                <section className="library-regular-section">
                  <div className="library-section-heading">
                    <div>
                      <LayoutGrid aria-hidden="true" />
                      <h2>{pinnedPage.matchedCount > 0 ? "其他网站" : "全部网站"}</h2>
                    </div>
                    <span>{regularPage.matchedCount} 个</span>
                  </div>
                  <SiteCollection sites={regularPage.items} {...collectionProps} />
                  {regularPage.nextCursor && (
                    <button
                      className="library-load-more"
                      type="button"
                      onClick={() => void loadMore("regular")}
                      disabled={regularPage.loadingMore}
                    >
                      {regularPage.loadingMore && <LoaderCircle className="loading-spinner" aria-hidden="true" />}
                      {regularPage.loadingMore ? "正在加载" : "加载更多网站"}
                    </button>
                  )}
                </section>
              )}
            </div>
          )}
        </div>
      </div>

      <LibraryDialog
        open={dialog?.kind === "create" || dialog?.kind === "edit"}
        title={dialog?.kind === "edit" ? "编辑网站" : "新增网站"}
        description={dialog?.kind === "edit" ? "修改资料库中的网站信息。" : "保存一个网站到当前账号的资料库。"}
        size="wide"
        onClose={closeDialog}
      >
        {dialog?.kind === "create" && (
          <SiteForm
            categories={categories}
            tags={tags}
            busy={mutationBusy}
            error={mutationError}
            onCancel={closeDialog}
            onCreate={handleCreate}
          />
        )}
        {dialog?.kind === "edit" && (
          <SiteForm
            site={dialog.site}
            categories={categories}
            tags={tags}
            busy={mutationBusy}
            error={mutationError}
            onCancel={closeDialog}
            onUpdate={handleUpdate}
          />
        )}
      </LibraryDialog>

      <LibraryDialog
        open={dialog?.kind === "delete"}
        title="删除网站"
        description="此操作会从当前账号的资料库中移除该网站。"
        onClose={closeDialog}
      >
        {dialog?.kind === "delete" && (
          <div className="library-delete-confirmation">
            <div className="library-delete-site">
              <SiteFavicon url={dialog.site.faviconUrl} name={dialog.site.name} size={32} />
              <div>
                <strong>{dialog.site.name}</strong>
                <span>{siteHost(dialog.site)}</span>
              </div>
            </div>
            <p>删除后，该网站将不再出现在资料库和关联浏览视图中。</p>
            {mutationError && <p className="library-form-error" role="alert">{mutationError}</p>}
            <footer className="library-form-actions">
              <button className="library-button secondary" type="button" onClick={closeDialog} disabled={mutationBusy}>取消</button>
              <button className="library-button danger" type="button" onClick={() => void handleDelete()} disabled={mutationBusy}>
                {mutationBusy ? <LoaderCircle className="loading-spinner" aria-hidden="true" /> : <Trash2 aria-hidden="true" />}
                {mutationBusy ? "正在删除" : "确认删除"}
              </button>
            </footer>
          </div>
        )}
      </LibraryDialog>

      <LibraryDialog
        open={dialog?.kind === "taxonomy"}
        title="管理分类与标签"
        description="分类删除前会预览受影响的网站；标签删除也会显示关联数量。"
        size="wide"
        onClose={closeDialog}
      >
        {dialog?.kind === "taxonomy" && (
          <TaxonomyManager categories={categories} tags={tags} onChanged={handleTaxonomyChanged} />
        )}
      </LibraryDialog>
    </main>
  );
}
