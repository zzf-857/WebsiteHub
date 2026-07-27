"use client";

import {
  AlertTriangle,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import {
  SiteFavicon,
} from "@/components/site-favicon";
import {
  OpenAllDialog,
} from "@/components/spaces/open-all-dialog";
import {
  SpaceDialog,
} from "@/components/spaces/space-dialog";
import {
} from "@/lib/space-contract";
import {
  SpaceNameForm,
  siteHost,
} from "@/components/spaces/space-workspace-parts";
import { useSpaceWorkspace } from "@/components/spaces/use-space-workspace";

export function SpaceWorkspace({
  initialSpaceId = null,
}: Readonly<{ initialSpaceId?: string | null }>) {
  const {
    closeDialog,
    detail,
    dialog,
    handleCreate,
    handleDelete,
    handleRemoveMember,
    handleRename,
    loadDeletePreview,
    mutationBusy,
    mutationError,
    openAllTarget,
    renderDetail,
    renderList,
    selectedSpaceId,
    setOpenAllTarget,
  } = useSpaceWorkspace(initialSpaceId);

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
        description="删除只会解除网站与 Space 的关系，不会删除资料库中的网站。"
        onClose={closeDialog}
      >
        {dialog?.kind === "delete" && (
          <div className="space-delete-confirmation">
            {dialog.previewLoading ? (
              <div className="space-preview-loading" aria-busy="true">
                <LoaderCircle className="loading-spinner" aria-hidden="true" />
                <span>正在计算删除影响</span>
              </div>
            ) : dialog.preview ? (
              <div className="space-delete-impact">
                <span className="space-warning-icon" aria-hidden="true"><AlertTriangle /></span>
                <div>
                  <strong>将删除“{dialog.preview.space.name}”</strong>
                  <p>
                    将解除 {dialog.preview.affectedSiteCount} 个网站的 Space 关系；这些网站仍会保留在资料库中。
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
                {mutationBusy && <LoaderCircle className="loading-spinner" aria-hidden="true" />}
                {mutationBusy ? "正在删除" : "确认删除"}
              </button>
            </div>
          </div>
        )}
      </SpaceDialog>

      <SpaceDialog
        open={dialog?.kind === "remove"}
        title="移出 Space"
        description="网站本身仍会保留在资料库和其他 Space 中。"
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
                {mutationBusy && <LoaderCircle className="loading-spinner" aria-hidden="true" />}
                {mutationBusy ? "正在移除" : "确认移出"}
              </button>
            </div>
          </div>
        )}
      </SpaceDialog>
    </>
  );
}
