"use client";

import { Check, Edit3, FolderTree, LoaderCircle, Plus, Tags, Trash2, X } from "lucide-react";
import { useState, type FormEvent } from "react";

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

  const items = kind === "categories" ? categories : tags;
  const maxNameLength = kind === "categories" ? MAX_LIBRARY_CATEGORY_NAME_LENGTH : MAX_LIBRARY_TAG_NAME_LENGTH;

  const run = async (operation: () => Promise<void>) => {
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

  const handleCreate = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    void run(async () => {
      if (kind === "categories") await createLibraryCategory(name);
      else await createLibraryTag(name);
      setNewName("");
      await onChanged();
    });
  };

  const handleRename = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editing?.name.trim()) return;
    void run(async () => {
      if (kind === "categories") await updateLibraryCategory(editing.id, editing.name);
      else await updateLibraryTag(editing.id, editing.name);
      setEditing(null);
      await onChanged();
    });
  };

  const prepareDeleteCategory = (category: LibraryCategory) => {
    void run(async () => {
      const preview = await previewLibraryCategoryDelete(category.id);
      setDeleteCandidate({ kind: "category", preview });
    });
  };

  const confirmDelete = () => {
    if (!deleteCandidate) return;
    void run(async () => {
      if (deleteCandidate.kind === "category") {
        await deleteLibraryCategory(deleteCandidate.preview.category.id);
      } else {
        await deleteLibraryTag(deleteCandidate.tag.id);
      }
      setDeleteCandidate(null);
      await onChanged();
    });
  };

  return (
    <div className="taxonomy-manager">
      <div className="taxonomy-tabs" role="tablist" aria-label="管理内容">
        <button
          type="button"
          role="tab"
          aria-selected={kind === "categories"}
          data-active={kind === "categories" || undefined}
          onClick={() => { setKind("categories"); setEditing(null); setDeleteCandidate(null); }}
        >
          <FolderTree aria-hidden="true" /> 分类
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={kind === "tags"}
          data-active={kind === "tags" || undefined}
          onClick={() => { setKind("tags"); setEditing(null); setDeleteCandidate(null); }}
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
            >
              {busy ? <LoaderCircle className="loading-spinner" aria-hidden="true" /> : <Trash2 aria-hidden="true" />}
              删除
            </button>
          </div>
        </section>
      )}

      <div className="taxonomy-list" role="tabpanel">
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
