"use client";

import { Check, Edit3, FolderTree, Merge, Plus, RotateCcw, Tags, Trash2, X } from "lucide-react";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { Spinner } from "@/components/react-bits/spinner";
import { DynamicIcon } from "@/components/ui/dynamic-icon";
import { ThemedSelect, type ThemedSelectOption } from "@/components/ui/themed-select";
import { CATEGORY_ICON_OPTIONS, categoryIconLabel } from "@/lib/category-icons";

import { MAX_LIBRARY_CATEGORY_NAME_LENGTH, MAX_LIBRARY_TAG_NAME_LENGTH } from "@/lib/library-contract";
import {
  createLibraryCategory,
  createLibraryTag,
  deleteLibraryCategory,
  deleteLibraryTag,
  mergeLibraryTag,
  previewLibraryCategoryDelete,
  previewLibraryTagMerge,
  updateLibraryCategory,
  updateLibraryTag,
} from "@/lib/library-client";
import type {
  LibraryCategory,
  LibraryCategoryDeletePreview,
  LibraryTag,
  LibraryTagMergePreview,
} from "@/lib/library-contract";
import { tagMergeSummary, tagMergeTargetOptions } from "@/lib/tag-merge";

type TaxonomyKind = "categories" | "tags";

type DeleteCandidate =
  | { kind: "category"; preview: LibraryCategoryDeletePreview }
  | { kind: "tag"; tag: LibraryTag };

type MergeState = {
  source: LibraryTag;
  targetId: string;
  preview: LibraryTagMergePreview | null;
  previewError: string | null;
};

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
  const [iconPickerId, setIconPickerId] = useState<string | null>(null);
  const [merge, setMerge] = useState<MergeState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const categoryTabRef = useRef<HTMLButtonElement>(null);
  const tagTabRef = useRef<HTMLButtonElement>(null);
  const tabIdPrefix = useId();
  const iconTriggerRefs = useRef(new Map<string, HTMLButtonElement>());
  // 选好图标后选择器收起：保存期间触发按钮是禁用的，等 busy 结束再把焦点还给它
  const focusTriggerRef = useRef<string | null>(null);

  useEffect(() => {
    if (busy || !focusTriggerRef.current) return;
    iconTriggerRefs.current.get(focusTriggerRef.current)?.focus();
    focusTriggerRef.current = null;
  }, [busy, categories]);
  const mergeSourceId = merge?.source.id ?? null;
  const mergeTargetId = merge?.targetId ?? "";

  // 选定目标后读取预览：受影响收藏数是二次确认的依据。切换目标时丢弃过期响应。
  useEffect(() => {
    if (!mergeSourceId || !mergeTargetId) return;
    const controller = new AbortController();
    previewLibraryTagMerge(mergeSourceId, mergeTargetId, controller.signal)
      .then((preview) => {
        if (controller.signal.aborted) return;
        setMerge((current) =>
          current?.source.id === mergeSourceId && current.targetId === mergeTargetId
            ? { ...current, preview, previewError: null }
            : current,
        );
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return;
        setMerge((current) =>
          current?.source.id === mergeSourceId && current.targetId === mergeTargetId
            ? { ...current, preview: null, previewError: caught instanceof Error ? caught.message : "预览没有读出来，请重试" }
            : current,
        );
      });
    return () => controller.abort();
  }, [mergeSourceId, mergeTargetId]);

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
    setIconPickerId(null);
    setMerge(null);
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
    setIconPickerId(null);
    void runRead(async () => {
      const preview = await previewLibraryCategoryDelete(category.id);
      setDeleteCandidate({ kind: "category", preview });
    });
  };

  const closeIconPicker = (categoryId: string) => {
    setIconPickerId(null);
    iconTriggerRefs.current.get(categoryId)?.focus();
  };

  /** icon 为 "" 时恢复按名称自动推断。 */
  const chooseIcon = (category: LibraryCategory, icon: string) => {
    if (icon === category.icon) {
      closeIconPicker(category.id);
      return;
    }
    void runMutation(
      async () => { await updateLibraryCategory(category.id, category.name, icon); },
      () => {
        focusTriggerRef.current = category.id;
        setIconPickerId(null);
      },
    );
  };

  const startMerge = (tag: LibraryTag) => {
    setDeleteCandidate(null);
    setEditing(null);
    setError(null);
    setMerge({ source: tag, targetId: "", preview: null, previewError: null });
  };

  const confirmMerge = () => {
    const target = merge;
    if (!target?.preview || target.preview.target.id !== target.targetId) return;
    void runMutation(
      async () => { await mergeLibraryTag(target.source.id, target.targetId); },
      () => setMerge(null),
    );
  };

  const mergeOptions: readonly ThemedSelectOption<string>[] = merge
    ? [{ value: "", label: "选择目标标签", disabled: true }, ...tagMergeTargetOptions(tags, merge.source.id)]
    : [];
  const mergeReady = Boolean(merge?.preview && merge.preview.target.id === merge.targetId);

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

      {merge && (
        <section className="taxonomy-delete-preview taxonomy-merge-panel" aria-labelledby={`${tabIdPrefix}-merge-title`}>
          <div className="taxonomy-merge-body">
            <strong id={`${tabIdPrefix}-merge-title`}>把「{merge.source.name}」合并到…</strong>
            <ThemedSelect
              className="taxonomy-merge-target"
              value={merge.targetId}
              options={mergeOptions}
              onValueChange={(targetId) => setMerge({ ...merge, targetId, preview: null, previewError: null })}
              ariaLabel={`「${merge.source.name}」要合并到的标签`}
              disabled={busy}
              searchable
            />
            <p aria-live="polite">
              {!merge.targetId && "合并后，带这个标签的收藏都会改带目标标签，这个标签随后删除。"}
              {merge.targetId && !merge.preview && !merge.previewError && "正在计算受影响的收藏…"}
              {merge.previewError}
              {merge.preview && mergeReady && tagMergeSummary(merge.preview)}
            </p>
          </div>
          <div className="taxonomy-confirm-actions">
            <button className="library-button secondary" type="button" onClick={() => setMerge(null)} disabled={busy}>取消</button>
            <button
              className="library-button primary"
              type="button"
              onClick={confirmMerge}
              disabled={busy || !mergeReady}
              aria-busy={busy || undefined}
            >
              {busy ? <Spinner /> : <Merge aria-hidden="true" />}
              {busy ? "正在合并" : "确认合并"}
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
          const category = kind === "categories" ? (item as LibraryCategory) : null;
          const pickerOpen = category !== null && iconPickerId === category.id;
          const pickerId = `${tabIdPrefix}-icons-${item.id}`;
          return (
            <div className="taxonomy-row-group" key={item.id}>
              <div className="taxonomy-row">
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
                    {category && (
                      <button
                        ref={(node) => {
                          if (node) iconTriggerRefs.current.set(category.id, node);
                          else iconTriggerRefs.current.delete(category.id);
                        }}
                        className="icon-button taxonomy-icon-trigger"
                        type="button"
                        aria-expanded={pickerOpen}
                        aria-controls={pickerOpen ? pickerId : undefined}
                        disabled={busy || isDefault}
                        onClick={() => setIconPickerId(pickerOpen ? null : category.id)}
                        aria-label={`更换「${category.name}」的图标，当前：${categoryIconLabel(category.icon)}`}
                        title={isDefault ? "默认分类不能修改" : "更换图标"}
                      >
                        <DynamicIcon name={category.icon} aria-hidden="true" />
                      </button>
                    )}
                    <div className="taxonomy-name">
                      <strong>{item.name}</strong>
                      <span>{item.siteCount} 个网站{isDefault ? " · 默认" : ""}</span>
                    </div>
                    <div className="taxonomy-row-actions">
                      {!category && (
                        <button
                          className="icon-button"
                          type="button"
                          onClick={() => startMerge(item as LibraryTag)}
                          disabled={busy || tags.length < 2}
                          aria-label={`把${item.name}合并到其它标签`}
                          title={tags.length < 2 ? "至少需要两个标签才能合并" : "合并到…"}
                        >
                          <Merge aria-hidden="true" />
                        </button>
                      )}
                      <button className="icon-button" type="button" onClick={() => setEditing({ id: item.id, name: item.name })} disabled={busy} aria-label={`重命名${item.name}`} title="重命名">
                        <Edit3 aria-hidden="true" />
                      </button>
                      <button
                        className="icon-button taxonomy-delete-button"
                        type="button"
                        disabled={busy || isDefault}
                        onClick={() => {
                          if (kind === "categories") prepareDeleteCategory(item as LibraryCategory);
                          else {
                            setMerge(null);
                            setDeleteCandidate({ kind: "tag", tag: item as LibraryTag });
                          }
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
              {pickerOpen && category && (
                <div
                  className="taxonomy-icon-picker"
                  id={pickerId}
                  role="group"
                  aria-label={`选择「${category.name}」的图标`}
                  onKeyDown={(event) => {
                    if (event.key !== "Escape") return;
                    event.preventDefault();
                    event.stopPropagation();
                    closeIconPicker(category.id);
                  }}
                >
                  {CATEGORY_ICON_OPTIONS.map((option) => (
                    <button
                      key={option.name}
                      className="taxonomy-icon-option"
                      type="button"
                      aria-pressed={category.icon === option.name}
                      data-active={category.icon === option.name || undefined}
                      disabled={busy}
                      onClick={() => chooseIcon(category, option.name)}
                      aria-label={option.label}
                      title={option.label}
                    >
                      <DynamicIcon name={option.name} aria-hidden="true" />
                    </button>
                  ))}
                  <button
                    className="taxonomy-icon-option taxonomy-icon-reset"
                    type="button"
                    disabled={busy}
                    onClick={() => chooseIcon(category, "")}
                    title="按分类名称自动选择图标"
                  >
                    <RotateCcw aria-hidden="true" />
                    自动
                  </button>
                </div>
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
