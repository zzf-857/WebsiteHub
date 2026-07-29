"use client";

import { Plus, Save } from "lucide-react";
import { useId, useMemo, useState, type FormEvent } from "react";

import { Spinner } from "@/components/react-bits/spinner";

import {
  libraryTagNameKey,
  MAX_LIBRARY_SITE_NAME_LENGTH,
  MAX_LIBRARY_SITE_SUMMARY_LENGTH,
  MAX_LIBRARY_TAG_NAME_LENGTH,
  MIN_LIBRARY_SITE_SUMMARY_LENGTH,
  normalizeLibraryTagName,
} from "@/lib/library-contract";
import type {
  LibraryCategory,
  LibrarySite,
  LibrarySiteCreateInput,
  LibrarySiteUpdateInput,
  LibraryTag,
} from "@/lib/library-contract";
import {
  buildLibrarySiteUpdate,
  type LibrarySiteFormValues,
} from "@/lib/library-site-form";

type SiteFormProps = {
  site?: LibrarySite;
  categories: LibraryCategory[];
  tags: LibraryTag[];
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onCreateTag: (name: string) => Promise<LibraryTag>;
  onCreate?: (input: LibrarySiteCreateInput) => Promise<void>;
  onUpdate?: (input: LibrarySiteUpdateInput) => Promise<void>;
};

function initialState(site: LibrarySite | undefined): LibrarySiteFormValues {
  return {
    name: site?.name ?? "",
    url: site?.originalUrl ?? "",
    summary: site?.summary ?? "",
    description: site?.description ?? "",
    faviconUrl: site?.faviconUrl ?? "",
    categoryId: site?.category.id ?? "",
    tagIds: site?.tags.map((tag) => tag.id) ?? [],
    pinned: site?.pinned ?? false,
  };
}

export function SiteForm(props: Readonly<SiteFormProps>) {
  const formKey = props.site ? `${props.site.id}:${props.site.version}` : "new";
  return <SiteFormContent key={formKey} {...props} />;
}

function SiteFormContent({
  site,
  categories,
  tags,
  busy,
  error,
  onCancel,
  onCreateTag,
  onCreate,
  onUpdate,
}: Readonly<SiteFormProps>) {
  const [values, setValues] = useState<LibrarySiteFormValues>(() => initialState(site));
  const [tagQuery, setTagQuery] = useState("");
  const [newTagName, setNewTagName] = useState("");
  const [tagCreating, setTagCreating] = useState(false);
  const [tagCreateError, setTagCreateError] = useState<string | null>(null);
  const errorId = useId();
  const tagCreateErrorId = useId();
  const isEdit = Boolean(site);

  const visibleTags = useMemo(() => {
    const query = libraryTagNameKey(tagQuery);
    return query ? tags.filter((tag) => libraryTagNameKey(tag.name).includes(query)) : tags;
  }, [tagQuery, tags]);

  const selectTag = (tag: LibraryTag) => {
    setValues((current) => ({
      ...current,
      tagIds: current.tagIds.includes(tag.id) ? current.tagIds : [...current.tagIds, tag.id],
    }));
  };

  const handleCreateTag = async () => {
    if (busy || tagCreating) return;
    const name = normalizeLibraryTagName(newTagName);
    if (!name) return;

    const existing = tags.find((tag) => libraryTagNameKey(tag.name) === libraryTagNameKey(name));
    if (existing) {
      selectTag(existing);
      setNewTagName("");
      setTagQuery("");
      setTagCreateError(null);
      return;
    }

    setTagCreating(true);
    setTagCreateError(null);
    try {
      const tag = await onCreateTag(name);
      selectTag(tag);
      setNewTagName("");
      setTagQuery("");
    } catch (caught) {
      setTagCreateError(caught instanceof Error ? caught.message : "新建标签失败，请重试");
    } finally {
      setTagCreating(false);
    }
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || tagCreating) return;
    if (site && onUpdate) {
      const update = buildLibrarySiteUpdate(site, values);
      if (update === null) {
        onCancel();
        return;
      }
      await onUpdate(update);
      return;
    }

    const shared = {
      name: values.name,
      url: values.url,
      summary: values.summary.trim() || null,
      description: values.description.trim() || null,
      faviconUrl: values.faviconUrl.trim() || null,
      tagIds: values.tagIds,
      pinned: values.pinned,
    };
    if (!site && onCreate) {
      await onCreate({
        ...shared,
        summary: shared.summary ?? undefined,
        description: shared.description ?? undefined,
        faviconUrl: shared.faviconUrl ?? undefined,
        categoryId: values.categoryId || undefined,
      });
    }
  };

  return (
    <form className="library-form" onSubmit={(event) => void handleSubmit(event)} aria-describedby={error ? errorId : undefined}>
      <div className="library-form-grid">
        <label className="library-field">
          <span>名称</span>
          <input
            required
            autoFocus
            maxLength={MAX_LIBRARY_SITE_NAME_LENGTH}
            value={values.name}
            onChange={(event) => setValues((current) => ({ ...current, name: event.target.value }))}
            disabled={busy}
          />
        </label>
        <label className="library-field library-field-wide">
          <span>网址</span>
          <input
            required
            type="url"
            inputMode="url"
            placeholder="https://example.com"
            value={values.url}
            onChange={(event) => setValues((current) => ({ ...current, url: event.target.value }))}
            disabled={busy}
          />
        </label>
        <label className="library-field library-field-wide">
          <span>简介</span>
          <textarea
            rows={2}
            minLength={MIN_LIBRARY_SITE_SUMMARY_LENGTH}
            maxLength={MAX_LIBRARY_SITE_SUMMARY_LENGTH}
            value={values.summary}
            onChange={(event) => setValues((current) => ({ ...current, summary: event.target.value }))}
            disabled={busy}
          />
        </label>
        <label className="library-field library-field-wide">
          <span>详细介绍</span>
          <textarea
            rows={4}
            maxLength={4000}
            value={values.description}
            onChange={(event) => setValues((current) => ({ ...current, description: event.target.value }))}
            disabled={busy}
          />
        </label>
        <label className="library-field library-field-wide">
          <span>Favicon URL <small>选填</small></span>
          <input
            type="url"
            inputMode="url"
            placeholder="https://example.com/favicon.ico"
            value={values.faviconUrl}
            onChange={(event) => setValues((current) => ({ ...current, faviconUrl: event.target.value }))}
            disabled={busy}
          />
        </label>
        <label className="library-field">
          <span>分类</span>
          <select
            value={values.categoryId}
            onChange={(event) => setValues((current) => ({ ...current, categoryId: event.target.value }))}
            disabled={busy}
          >
            <option value="">使用默认分类</option>
            {categories.map((category) => (
              <option key={category.id} value={category.id}>{category.name}</option>
            ))}
          </select>
        </label>
        <label className="library-check library-pin-check">
          <input
            type="checkbox"
            checked={values.pinned}
            onChange={(event) => setValues((current) => ({ ...current, pinned: event.target.checked }))}
            disabled={busy}
          />
          <span>置顶</span>
        </label>
      </div>

      <fieldset className="library-tag-picker" disabled={busy || tagCreating}>
        <legend>标签</legend>
        <div className="taxonomy-create">
          <label className="library-field">
            <span className="sr-only">新标签名称</span>
            <input
              maxLength={MAX_LIBRARY_TAG_NAME_LENGTH}
              placeholder="输入新标签名称"
              value={newTagName}
              aria-describedby={tagCreateError ? tagCreateErrorId : undefined}
              onChange={(event) => {
                setNewTagName(event.target.value);
                setTagCreateError(null);
              }}
              onKeyDown={(event) => {
                // 新建必须来自明确的按钮操作，不能让输入框 Enter 隐式写库。
                if (event.key === "Enter") event.preventDefault();
              }}
            />
          </label>
          <button
            className="library-button secondary"
            type="button"
            disabled={busy || tagCreating || !normalizeLibraryTagName(newTagName)}
            onClick={() => void handleCreateTag()}
          >
            {tagCreating ? (
              <Spinner />
            ) : (
              <Plus aria-hidden="true" />
            )}
            {tagCreating ? "正在新建" : "新建标签"}
          </button>
        </div>
        {tagCreateError && (
          <p className="library-form-error" id={tagCreateErrorId} role="alert">
            {tagCreateError}
          </p>
        )}
        {tags.length > 8 && (
          <label className="library-field library-tag-search">
            <span className="sr-only">筛选标签</span>
            <input
              type="search"
              placeholder="筛选标签"
              value={tagQuery}
              onChange={(event) => setTagQuery(event.target.value)}
            />
          </label>
        )}
        <div className="library-tag-options">
          {visibleTags.map((tag) => (
            <label className="library-check" key={tag.id}>
              <input
                type="checkbox"
                checked={values.tagIds.includes(tag.id)}
                onChange={(event) => {
                  setValues((current) => ({
                    ...current,
                    tagIds: event.target.checked
                      ? [...current.tagIds, tag.id]
                      : current.tagIds.filter((id) => id !== tag.id),
                  }));
                }}
              />
              <span>{tag.name}</span>
            </label>
          ))}
          {visibleTags.length === 0 && <p className="library-inline-empty">没有匹配的标签</p>}
        </div>
      </fieldset>

      {error && <p className="library-form-error" id={errorId} role="alert">{error}</p>}
      <footer className="library-form-actions">
        <button className="library-button secondary" type="button" onClick={onCancel} disabled={busy}>取消</button>
        <button className="library-button primary" type="submit" disabled={busy || tagCreating || !values.name.trim() || !values.url.trim()}>
          {busy ? <Spinner /> : <Save aria-hidden="true" />}
          <span>{busy ? "正在保存" : isEdit ? "保存修改" : "新增网站"}</span>
        </button>
      </footer>
    </form>
  );
}
