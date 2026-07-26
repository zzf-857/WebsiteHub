"use client";

import { LoaderCircle, Save } from "lucide-react";
import { useId, useMemo, useState, type FormEvent } from "react";

import { MAX_LIBRARY_SITE_NAME_LENGTH } from "@/lib/library-contract";
import type {
  LibraryCategory,
  LibrarySite,
  LibrarySiteCreateInput,
  LibrarySiteUpdateInput,
  LibraryTag,
} from "@/lib/library-contract";

type SiteFormProps = {
  site?: LibrarySite;
  categories: LibraryCategory[];
  tags: LibraryTag[];
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onCreate?: (input: LibrarySiteCreateInput) => Promise<void>;
  onUpdate?: (input: LibrarySiteUpdateInput) => Promise<void>;
};

type FormState = {
  name: string;
  url: string;
  description: string;
  faviconUrl: string;
  categoryId: string;
  tagIds: string[];
  pinned: boolean;
};

function initialState(site: LibrarySite | undefined): FormState {
  return {
    name: site?.name ?? "",
    url: site?.originalUrl ?? "",
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
  onCreate,
  onUpdate,
}: Readonly<SiteFormProps>) {
  const [values, setValues] = useState<FormState>(() => initialState(site));
  const [tagQuery, setTagQuery] = useState("");
  const errorId = useId();
  const isEdit = Boolean(site);

  const visibleTags = useMemo(() => {
    const query = tagQuery.trim().toLocaleLowerCase();
    return query ? tags.filter((tag) => tag.name.toLocaleLowerCase().includes(query)) : tags;
  }, [tagQuery, tags]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    const shared = {
      name: values.name,
      url: values.url,
      description: values.description.trim() || null,
      faviconUrl: values.faviconUrl.trim() || null,
      tagIds: values.tagIds,
      pinned: values.pinned,
    };
    if (site && onUpdate) {
      await onUpdate({ ...shared, categoryId: values.categoryId || null, expectedVersion: site.version });
    }
    if (!site && onCreate) {
      await onCreate({
        ...shared,
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
          <span>描述</span>
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

      <fieldset className="library-tag-picker" disabled={busy}>
        <legend>标签</legend>
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
        <button className="library-button primary" type="submit" disabled={busy || !values.name.trim() || !values.url.trim()}>
          {busy ? <LoaderCircle className="loading-spinner" aria-hidden="true" /> : <Save aria-hidden="true" />}
          <span>{busy ? "正在保存" : isEdit ? "保存修改" : "新增网站"}</span>
        </button>
      </footer>
    </form>
  );
}
