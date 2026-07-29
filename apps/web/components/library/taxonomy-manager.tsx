"use client";

import { Check, Edit3, FolderTree, Plus, Tags, Trash2, X } from "lucide-react";
import {
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { Spinner } from "@/components/react-bits/spinner";

import { MAX_LIBRARY_CATEGORY_NAME_LENGTH, MAX_LIBRARY_TAG_NAME_LENGTH } from "@/lib/library-contract";
import {
  createLibraryCategory,
  createLibraryTag,
  deleteLibraryCategory,
  deleteLibraryTag,
  previewLibraryCategoryDelete,
  updateLibraryCategory,
  updateLibraryTag,
} from "@/lib/library-client";
import type {
  LibraryCategory,
  LibraryCategoryDeletePreview,
  LibraryTag,
} from "@/lib/library-contract";

type TaxonomyKind = "categories" | "tags";

type DeleteCandidate =
  | { kind: "category"; preview: LibraryCategoryDeletePreview }
  | { kind: "tag"; tag: LibraryTag };

type TaxonomyManagerProps = {
  categories: LibraryCategory[];
  tags: LibraryTag[];
  onChanged: () => Promise<void>;
};

export function TaxonomyManager({ categories, tags, onChanged }: Readonly<TaxonomyManagerProps>) {
  const [kind, setKind] = useState<TaxonomyKind>("categories");
  const [newName, setNewName] = useState("");
  const [editing, setEditing] = useState<{ id: string; name: string } | null>(null);
  const [deleteCandidate, setDeleteCandidate] = useState<DeleteCandidate | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const categoryTabRef = useRef<HTMLButtonElement>(null);
  const tagTabRef = useRef<HTMLButtonElement>(null);
  const tabIdPrefix = useId();

  const items = kind === "categories" ? categories : tags;
  const maxNameLength = kind === "categories" ? MAX_LIBRARY_CATEGORY_NAME_LENGTH : MAX_LIBRARY_TAG_NAME_LENGTH;
  const categoryTabId = `${tabIdPrefix}-categories-tab`;
  const tagTabId = `${tabIdPrefix}-tags-tab`;
  const panelId = `${tabIdPrefix}-panel`;

  const runRead = async (operation: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await operation();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const runMutation = async (
    operation: () => Promise<void>,
    onSaved: () => void,
  ) => {
    setBusy(true);
    setError(null);
    try {
      await operation();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请重试");
      setBusy(false);
      return;
    }

    onSaved();
    try {
      await onChanged();
    } catch {
      setError("已保存但刷新失败，请勿重复提交；关闭窗口后使用页面上的重试按钮。");
    } finally {
      setBusy(false);
    }
  };

  const selectKind = (nextKind: TaxonomyKind) => {
    setKind(nextKind);
    setNewName("");
    setEditing(null);
    setDeleteCandidate(null);
    setError(null);
  };

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    currentKind: TaxonomyKind,
  ) => {
    let nextKind: TaxonomyKind | null = null;
    if (event.key === "ArrowRight") nextKind = currentKind === "categories" ? "tags" : "categories";
    if (event.key === "ArrowLeft") nextKind = currentKind === "categories" ? "tags" : "categories";
    if (event.key === "Home") nextKind = "categories";
    if (event.key === "End") nextKind = "tags";
    if (!nextKind) return;

    event.preventDefault();
    selectKind(nextKind);
    (nextKind === "categories" ? categoryTabRef : tagTabRef).current?.focus();
  };

  const handleCreate = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    const targetKind = kind;
    void runMutation(async () => {
      if (targetKind === "categories") await createLibraryCategory(name);
      else await createLibraryTag(name);
    }, () => setNewName(""));
  };

  const handleRename = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editing?.name.trim()) return;
    const targetKind = kind;
    const target = editing;
    void runMutation(async () => {
      if (targetKind === "categories") await updateLibraryCategory(target.id, target.name);
      else await updateLibraryTag(target.id, target.name);
    }, () => setEditing(null));
  };

  const prepareDeleteCategory = (category: LibraryCategory) => {
    void runRead(async () => {
      const preview = await previewLibraryCategoryDelete(category.id);
      setDeleteCandidate({ kind: "category", preview });
    });
  };

  const confirmDelete = () => {
    if (!deleteCandidate) return;
    const target = deleteCandidate;
    void runMutation(async () => {
      if (target.kind === "category") {
        await deleteLibraryCategory(target.preview.category.id);
      } else {
        await deleteLibraryTag(target.tag.id);
      }
    }, () => setDeleteCandidate(null));
  };

  return (
    <div className="taxonomy-manager">
      <div className="taxonomy-tabs" role="tablist" aria-label="管理内容" aria-orientation="horizontal">
        <button
          ref={categoryTabRef}
          id={categoryTabId}
          type="button"
          role="tab"
          aria-selected={kind === "categories"}
          aria-controls={panelId}
          tabIndex={kind === "categories" ? 0 : -1}
          data-active={kind === "categories" || undefined}
          disabled={busy}
          onClick={() => selectKind("categories")}
          onKeyDown={(event) => handleTabKeyDown(event, "categories")}
        >
          <FolderTree aria-hidden="true" /> 分类
        </button>
        <button
          ref={tagTabRef}
          id={tagTabId}
          type="button"
          role="tab"
          aria-selected={kind === "tags"}
          aria-controls={panelId}
          tabIndex={kind === "tags" ? 0 : -1}
          data-active={kind === "tags" || undefined}
          disabled={busy}
          onClick={() => selectKind("tags")}
          onKeyDown={(event) => handleTabKeyDown(event, "tags")}
        >
          <Tags aria-hidden="true" /> 标签
        </button>
      </div>

      <form className="taxonomy-create" onSubmit={handleCreate}>
        <label className="library-field">
          <span className="sr-only">新建{kind === "categories" ? "分类" : "标签"}</span>
          <input
            value={newName}
            maxLength={maxNameLength}
            placeholder={`新建${kind === "categories" ? "分类" : "标签"}`}
            onChange={(event) => setNewName(event.target.value)}
            disabled={busy}
          />
        </label>
        <button className="library-button primary" type="submit" disabled={busy || !newName.trim()}>
          <Plus aria-hidden="true" /> 新建
        </button>
      </form>

      {deleteCandidate && (
        <section className="taxonomy-delete-preview" aria-live="polite">
          <div>
            <strong>确认删除{deleteCandidate.kind === "category" ? "分类" : "标签"}</strong>
            {deleteCandidate.kind === "category" ? (
              <p>
                「{deleteCandidate.preview.category.name}」影响 {deleteCandidate.preview.affectedSiteCount} 个网站。
                {deleteCandidate.preview.replacementCategory
                  ? ` 删除后将移至「${deleteCandidate.preview.replacementCategory.name}」。`
                  : " 删除前请先移动相关网站。"}
              </p>
            ) : (
              <p>「{deleteCandidate.tag.name}」当前关联 {deleteCandidate.tag.siteCount} 个网站。</p>
            )}
          </div>
          <div className="taxonomy-confirm-actions">
            <button className="library-button secondary" type="button" onClick={() => setDeleteCandidate(null)} disabled={busy}>取消</button>
            <button
              className="library-button danger"
              type="button"
              onClick={confirmDelete}
              disabled={busy || (deleteCandidate.kind === "category" && !deleteCandidate.preview.replacementCategory && deleteCandidate.preview.affectedSiteCount > 0)}
              aria-busy={busy || undefined}
            >
              {busy ? <Spinner /> : <Trash2 aria-hidden="true" />}
              {busy ? "正在删除" : "删除"}
            </button>
          </div>
        </section>
      )}

      <div
        className="taxonomy-list"
        id={panelId}
        role="tabpanel"
        aria-labelledby={kind === "categories" ? categoryTabId : tagTabId}
        tabIndex={0}
      >
        {items.map((item) => {
          const isDefault = "isDefault" in item && item.isDefault === true;
          const isEditing = editing?.id === item.id;
          return (
            <div className="taxonomy-row" key={item.id}>
              {isEditing ? (
                <form className="taxonomy-rename" onSubmit={handleRename}>
                  <label className="library-field">
                    <span className="sr-only">新名称</span>
                    <input
                      autoFocus
                      maxLength={maxNameLength}
                      value={editing.name}
                      onChange={(event) => setEditing({ ...editing, name: event.target.value })}
                      disabled={busy}
                    />
                  </label>
                  <button className="icon-button" type="submit" disabled={busy || !editing.name.trim()} aria-label="保存名称" title="保存名称">
                    <Check aria-hidden="true" />
                  </button>
                  <button className="icon-button" type="button" onClick={() => setEditing(null)} disabled={busy} aria-label="取消重命名" title="取消重命名">
                    <X aria-hidden="true" />
                  </button>
                </form>
              ) : (
                <>
                  <div className="taxonomy-name">
                    <strong>{item.name}</strong>
                    <span>{item.siteCount} 个网站{isDefault ? " · 默认" : ""}</span>
                  </div>
                  <div className="taxonomy-row-actions">
                    <button className="icon-button" type="button" onClick={() => setEditing({ id: item.id, name: item.name })} disabled={busy} aria-label={`重命名${item.name}`} title="重命名">
                      <Edit3 aria-hidden="true" />
                    </button>
                    <button
                      className="icon-button taxonomy-delete-button"
                      type="button"
                      disabled={busy || isDefault}
                      onClick={() => {
                        if (kind === "categories") prepareDeleteCategory(item as LibraryCategory);
                        else setDeleteCandidate({ kind: "tag", tag: item as LibraryTag });
                      }}
                      aria-label={`删除${item.name}`}
                      title={isDefault ? "默认分类不能删除" : "删除"}
                    >
                      <Trash2 aria-hidden="true" />
                    </button>
                  </div>
                </>
              )}
            </div>
          );
        })}
        {items.length === 0 && <p className="library-inline-empty">暂无{kind === "categories" ? "分类" : "标签"}</p>}
      </div>
      {error && <p className="library-form-error" role="alert">{error}</p>}
    </div>
  );
}
