"use client";

import {
  AlertTriangle,
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Blocks,
  ExternalLink,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  Users,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  SpaceCard,
  SpaceMemberRow,
} from "@/components/spaces/space-workspace-parts";
import type {
  MemberMove,
} from "@/components/spaces/space-workspace-parts";
import type {
  SpaceSort,
} from "@/lib/space-contract";
import {
  SiteFavicon,
} from "@/components/site-favicon";
import { Spinner } from "@/components/react-bits/spinner";
import {
  OpenAllDialog,
} from "@/components/spaces/open-all-dialog";
import {
  SpaceDialog,
} from "@/components/spaces/space-dialog";
import {
  SpaceNameForm,
  siteHost,
} from "@/components/spaces/space-workspace-parts";
import { useSpaceWorkspace } from "@/components/spaces/use-space-workspace";
import { ThemedSelect } from "@/components/ui/themed-select";

const SPACE_SORT_OPTIONS: ReadonlyArray<{ value: SpaceSort; label: string }> = [
  { value: "updated", label: "最近更新" },
  { value: "created", label: "创建时间" },
  { value: "name", label: "名称" },
];

export function SpaceWorkspace({
  initialSpaceId = null,
}: Readonly<{ initialSpaceId?: string | null }>) {
  const {
    closeDialog,
    detail,
    detailError,
    detailLoading,
    detailLoadingMore,
    dialog,
    direction,
    handleCreate,
    handleDelete,
    handleMoveMember,
    handleRemoveMember,
    handleRename,
    loadDeletePreview,
    loadMoreMembers,
    loadMoreSpaces,
    memberBusyId,
    mutationBusy,
    mutationError,
    notice,
    openAllTarget,
    openDialog,
    refreshDetail,
    refreshList,
    returnToList,
    selectedSpaceId,
    setDirection,
    setNotice,
    setOpenAllTarget,
    setSelectedSpaceId,
    setSort,
    sort,
    spacePage,
    spacesError,
    spacesLoading,
  } = useSpaceWorkspace(initialSpaceId);
  const router = useRouter();


  const renderNotice = () => notice && (
    <div className="space-notice" role="status">
      <span>{notice}</span>
      <button type="button" onClick={() => setNotice(null)}>关闭</button>
    </div>
  );

  const renderDetail = () => (
    <main className="site-main space-workspace">
      <button className="space-back-button" type="button" onClick={returnToList}>
        <ArrowLeft aria-hidden="true" />
        返回 Space 列表
      </button>

      {renderNotice()}

      {detailLoading && !detail ? (
        <section className="space-detail-loading" aria-label="正在加载 Space" aria-busy="true">
          <Spinner />
          <span>正在加载 Space</span>
        </section>
      ) : detailError && !detail ? (
        <section className="space-state" role="alert">
          <span className="space-state-icon"><AlertTriangle aria-hidden="true" /></span>
          <h1>Space 加载失败</h1>
          <p>{detailError}</p>
          <button className="space-button primary" type="button" onClick={refreshDetail}>
            <RefreshCw aria-hidden="true" />
            重试
          </button>
        </section>
      ) : detail ? (
        <>
          <header className="workspace-page-header space-detail-header">
            <div>
              <span className="page-kicker">Space</span>
              <h1>{detail.name}</h1>
              <p>{detail.memberCount} 个网站，按自定义顺序排列。</p>
            </div>
            <div className="space-page-actions">
              <button
                className="space-button secondary"
                type="button"
                disabled={detail.memberCount === 0}
                onClick={() => setOpenAllTarget(detail)}
                title={detail.memberCount === 0 ? "这个 Space 还没有网站" : "一次打开其中全部网站"}
              >
                <ExternalLink aria-hidden="true" />
                全部打开
              </button>
              <button className="space-button secondary" type="button" onClick={() => openDialog({ kind: "rename", space: detail })}>
                <Pencil aria-hidden="true" />
                重命名
              </button>
              <button className="space-button danger-outline" type="button" onClick={() => void loadDeletePreview(detail)}>
                <Trash2 aria-hidden="true" />
                删除 Space
              </button>
            </div>
          </header>

          {detailError && (
            <div className="space-error-banner" role="alert">
              <AlertTriangle aria-hidden="true" />
              <span>{detailError}</span>
              <button type="button" onClick={refreshDetail}>
                <RefreshCw aria-hidden="true" />
                重新加载
              </button>
            </div>
          )}

          <section className="space-member-group" aria-labelledby="space-members-title">
            <header className="space-section-heading">
              <div>
                <Users aria-hidden="true" />
                <h2 id="space-members-title">Space 成员</h2>
              </div>
              <span>已加载 {detail.members.length} / {detail.memberCount}</span>
            </header>

            {detail.members.length === 0 ? (
              <div className="space-empty-state">
                <span className="space-empty-icon" aria-hidden="true"><Blocks /></span>
                <h2>这个 Space 还是空的</h2>
                <p>从网址库的网站详情中将网站加入这个 Space。</p>
                <Link className="space-button primary" href="/library">
                  前往网址库
                </Link>
              </div>
            ) : (
              <ol className="space-member-list">
                {detail.members.map((member, index) => (
                  <SpaceMemberRow
                    key={member.site.id}
                    member={member}
                    order={index}
                    totalCount={detail.memberCount}
                    busy={memberBusyId !== null}
                    onMove={(move: MemberMove) => void handleMoveMember(member.site.id, move)}
                    onRemove={() => openDialog({ kind: "remove", member })}
                  />
                ))}
              </ol>
            )}

            {detail.nextCursor && (
              <button className="space-load-more" type="button" disabled={detailLoadingMore || memberBusyId !== null} onClick={() => void loadMoreMembers()}>
                {detailLoadingMore && <Spinner />}
                {detailLoadingMore ? "正在加载" : "加载更多成员"}
              </button>
            )}
          </section>
        </>
      ) : null}
    </main>
  );

  const renderList = () => (
    <main className="site-main space-workspace">
      <header className="workspace-page-header space-page-header">
        <div>
          <span className="page-kicker">WebHub</span>
          <h1>Space</h1>
          <p>按项目或使用场景组织网站，每个网站可以属于多个 Space。</p>
        </div>
        <button className="space-button primary" type="button" onClick={() => openDialog({ kind: "create" })}>
          <Plus aria-hidden="true" />
          新建 Space
        </button>
      </header>

      <div className="space-toolbar">
        <div className="space-results-summary">
          <Blocks aria-hidden="true" />
          <span>共 {spacePage.totalCount} 个 Space</span>
        </div>
        <div className="space-sort-field">
          <ThemedSelect<SpaceSort>
            ariaLabel="Space 排序方式"
            className="space-toolbar-select"
            options={SPACE_SORT_OPTIONS}
            variant="toolbar"
            value={sort}
            onValueChange={setSort}
          />
        </div>
        <button
          className="icon-button space-direction-button"
          type="button"
          onClick={() => setDirection((current) => current === "asc" ? "desc" : "asc")}
          aria-label={direction === "asc" ? "当前升序，切换为降序" : "当前降序，切换为升序"}
          title={direction === "asc" ? "升序" : "降序"}
        >
          {direction === "asc" ? <ArrowUp aria-hidden="true" /> : <ArrowDown aria-hidden="true" />}
        </button>
      </div>

      {renderNotice()}

      {spacesError && (
        <div className="space-error-banner" role="alert">
          <AlertTriangle aria-hidden="true" />
          <span>{spacesError}</span>
          <button type="button" onClick={refreshList}>
            <RefreshCw aria-hidden="true" />
            重新加载
          </button>
        </div>
      )}

      {spacesLoading ? (
        <div className="space-card-list" aria-label="正在加载 Space" aria-busy="true">
          {Array.from({ length: 4 }, (_, index) => (
            <div className="space-card-skeleton" key={index} />
          ))}
        </div>
      ) : spacePage.items.length === 0 && !spacesError ? (
        <section className="space-empty-state">
          <span className="space-empty-icon" aria-hidden="true"><Blocks /></span>
          <h2>暂无 Space</h2>
          <p>创建一个 Space，把同一项目或场景会用到的网站整理在一起。</p>
          <button className="space-button primary" type="button" onClick={() => openDialog({ kind: "create" })}>
            <Plus aria-hidden="true" />
            创建第一个 Space
          </button>
        </section>
      ) : (
        <section className="space-collection" aria-labelledby="space-collection-title">
          <header className="space-section-heading">
            <div>
              <Blocks aria-hidden="true" />
              <h2 id="space-collection-title">我的 Space</h2>
            </div>
            <span>已加载 {spacePage.items.length} / {spacePage.totalCount}</span>
          </header>
          <div className="space-card-list">
            {spacePage.items.map((space) => (
              <SpaceCard
                key={space.id}
                space={space}
                onOpen={() => {
                  setNotice(null);
                  setSelectedSpaceId(space.id);
                  router.push(`/spaces/${encodeURIComponent(space.id)}`);
                }}
                onOpenAll={() => setOpenAllTarget(space)}
                onRename={() => openDialog({ kind: "rename", space })}
                onDelete={() => void loadDeletePreview(space)}
              />
            ))}
          </div>
          {spacePage.nextCursor && (
            <button className="space-load-more" type="button" disabled={spacePage.loadingMore} onClick={() => void loadMoreSpaces()}>
              {spacePage.loadingMore && <Spinner />}
              {spacePage.loadingMore ? "正在加载" : "加载更多 Space"}
            </button>
          )}
        </section>
      )}
    </main>
  );

  return (
    <>
      {selectedSpaceId ? renderDetail() : renderList()}

      {openAllTarget && (
        <OpenAllDialog space={openAllTarget} onClose={() => setOpenAllTarget(null)} />
      )}

      <SpaceDialog
        open={dialog?.kind === "create"}
        title="新建 Space"
        description="Space 用来组织同一项目或使用场景需要的网站。"
        closeDisabled={mutationBusy}
        onClose={closeDialog}
      >
        {dialog?.kind === "create" && (
          <SpaceNameForm
            busy={mutationBusy}
            error={mutationError}
            submitLabel="创建 Space"
            onCancel={closeDialog}
            onSubmit={handleCreate}
          />
        )}
      </SpaceDialog>

      <SpaceDialog
        open={dialog?.kind === "rename"}
        title="重命名 Space"
        description="名称在当前账号内不能与其他 Space 重复。"
        closeDisabled={mutationBusy}
        onClose={closeDialog}
      >
        {dialog?.kind === "rename" && (
          <SpaceNameForm
            key={`${dialog.space.id}:${dialog.space.version}`}
            initialName={dialog.space.name}
            busy={mutationBusy}
            error={mutationError}
            submitLabel="保存名称"
            onCancel={closeDialog}
            onSubmit={handleRename}
          />
        )}
      </SpaceDialog>

      <SpaceDialog
        open={dialog?.kind === "delete"}
        title="删除 Space"
        description="删除只会解除网站与 Space 的关系，不会删除网址库中的网站。"
        closeDisabled={mutationBusy}
        onClose={closeDialog}
      >
        {dialog?.kind === "delete" && (
          <div className="space-delete-confirmation">
            {dialog.previewLoading ? (
              <div className="space-preview-loading" aria-busy="true">
                <Spinner />
                <span>正在计算删除影响</span>
              </div>
            ) : dialog.preview ? (
              <div className="space-delete-impact">
                <span className="space-warning-icon" aria-hidden="true"><AlertTriangle /></span>
                <div>
                  <strong>将删除“{dialog.preview.space.name}”</strong>
                  <p>
                    将解除 {dialog.preview.affectedSiteCount} 个网站的 Space 关系；这些网站仍会保留在网址库中。
                  </p>
                </div>
              </div>
            ) : (
              <p className="space-inline-empty">暂时无法读取删除影响。</p>
            )}
            {mutationError && <p className="space-form-error" role="alert">{mutationError}</p>}
            <div className="space-form-actions">
              {!dialog.previewLoading && !dialog.preview && (
                <button className="space-button secondary" type="button" disabled={mutationBusy} onClick={() => void loadDeletePreview(dialog.space)}>
                  <RefreshCw aria-hidden="true" />
                  重试预览
                </button>
              )}
              <button className="space-button secondary" type="button" disabled={mutationBusy} onClick={closeDialog}>
                取消
              </button>
              <button className="space-button danger" type="button" disabled={mutationBusy || dialog.previewLoading || !dialog.preview} onClick={() => void handleDelete()}>
                {mutationBusy && <Spinner />}
                {mutationBusy ? "正在删除" : "确认删除"}
              </button>
            </div>
          </div>
        )}
      </SpaceDialog>

      <SpaceDialog
        open={dialog?.kind === "remove"}
        title="移出 Space"
        description="网站本身仍会保留在网址库和其他 Space 中。"
        closeDisabled={mutationBusy}
        onClose={closeDialog}
      >
        {dialog?.kind === "remove" && (
          <div className="space-remove-confirmation">
            <div className="space-remove-site">
              <SiteFavicon url={dialog.member.site.faviconUrl} name={dialog.member.site.name} size={32} />
              <div>
                <strong>{dialog.member.site.name}</strong>
                <span>{siteHost(dialog.member.site.originalUrl)}</span>
              </div>
            </div>
            <p>确认将这个网站从“{detail?.name ?? "当前 Space"}”移出吗？</p>
            {mutationError && <p className="space-form-error" role="alert">{mutationError}</p>}
            <div className="space-form-actions">
              <button className="space-button secondary" type="button" disabled={mutationBusy} onClick={closeDialog}>
                取消
              </button>
              <button className="space-button danger" type="button" disabled={mutationBusy} onClick={() => void handleRemoveMember()}>
                {mutationBusy && <Spinner />}
                {mutationBusy ? "正在移除" : "确认移出"}
              </button>
            </div>
          </div>
        )}
      </SpaceDialog>
    </>
  );
}
