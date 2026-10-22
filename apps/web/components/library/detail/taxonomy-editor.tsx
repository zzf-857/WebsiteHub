"use client";

import { Check, ChevronDown, FolderOpen, Plus, RefreshCw, Search, Tag } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import { Spinner } from "@/components/react-bits/spinner";
import {
  createLibraryTagResolvingConflict,
  listLibraryCategories,
  listLibraryTags,
} from "@/lib/library-client";
import type {
  LibraryCategory,
  LibrarySite,
  LibrarySiteUpdateInput,
  LibraryTag,
  LibraryTagRef,
} from "@/lib/library-contract";
import {
  categoryUpdateInput,
  exactTagOption,
  filterTagOptions,
  sortCategoryOptions,
  tagCreateCandidate,
  tagsUpdateInput,
  toggleTagSelection,
} from "@/lib/site-taxonomy-edit";

import { useDismissable } from "./use-dismissable";

// 详情页的分类与标签就地编辑（Q46）：点开小浮层选择，不再进完整编辑表单。
// 新建标签复用 createLibraryTagResolvingConflict（规范化同名与 409 并发冲突都会落到已有标签上）；
// 写入走页面的 onUpdate，版本冲突与“已被删除”的处理和编辑表单是同一条路径。

type TaxonomyEditorProps = {
  site: LibrarySite;
  /** 成功返回 true；失败时页面已经给出提示。 */
  onUpdate: (input: LibrarySiteUpdateInput) => Promise<boolean>;
};

type Loadable<T> =
  | { status: "idle" | "loading" }
  | { status: "ready"; items: T[] }
  | { status: "error"; message: string };

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function useTaxonomyList<T>(load: (signal: AbortSignal) => Promise<T[]>, active: boolean) {
  const [state, setState] = useState<Loadable<T>>({ status: "idle" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void load(controller.signal)
      .then((items) => setState({ status: "ready", items }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: errorText(error, "加载失败，请重试") });
      });
    return () => controller.abort();
  }, [active, attempt, load]);

  const retry = useCallback(() => {
    setState({ status: "loading" });
    setAttempt((current) => current + 1);
  }, []);

  return { state, setState, retry };
}

function CategoryPicker({ site, onUpdate }: Readonly<TaxonomyEditorProps>) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listId = useId();
  const { state } = useTaxonomyList<LibraryCategory>(listLibraryCategories, open);

  useDismissable(open, rootRef, triggerRef, () => setOpen(false));

  const choose = async (categoryId: string) => {
    const input = categoryUpdateInput(site, categoryId);
    if (!input) {
      setOpen(false);
      return;
    }
    setSaving(true);
    const saved = await onUpdate(input);
    setSaving(false);
    if (saved) {
      setOpen(false);
      triggerRef.current?.focus();
    }
  };

  return (
    <div className="sd-picker" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="sd-picker-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        disabled={saving}
        onClick={() => setOpen((current) => !current)}
      >
        <FolderOpen aria-hidden="true" />
        <span>{site.category.name}</span>
        {saving ? <Spinner size={13} /> : <ChevronDown aria-hidden="true" />}
      </button>
      {open && (
        <div className="sd-popover" data-shortcut-block>
          {state.status === "error" ? (
            <p className="sd-popover-note" role="alert">{state.message}</p>
          ) : state.status !== "ready" ? (
            <p className="sd-popover-note" role="status"><Spinner size={13} />正在读取分类…</p>
          ) : (
            <ul className="sd-popover-list" id={listId} role="listbox" aria-label="选择分类">
              {sortCategoryOptions(state.items).map((category) => {
                const selected = category.id === site.category.id;
                return (
                  <li key={category.id}>
                    <button
                      type="button"
                      role="option"
                      aria-selected={selected}
                      className="sd-popover-option"
                      disabled={saving}
                      onClick={() => void choose(category.id)}
                    >
                      <span className="sd-popover-check" aria-hidden="true">
                        {selected && <Check />}
                      </span>
                      <span className="sd-popover-label">{category.name}</span>
                      <small>{category.siteCount}</small>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function TagPicker({ site, onUpdate }: Readonly<TaxonomyEditorProps>) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState<string[]>([]);
  // 新建的标签在列表刷新前也要能显示名称
  const [extraTags, setExtraTags] = useState<LibraryTagRef[]>([]);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = useId();
  const { state, setState, retry } = useTaxonomyList<LibraryTag>(listLibraryTags, open);

  const openPicker = () => {
    setDraft(site.tags.map((tag) => tag.id));
    setQuery("");
    setCreateError(null);
    setExtraTags([]);
    setOpen(true);
  };

  /** 关闭即保存：点完成、点浮层外都会提交；Esc 放弃本次改动。 */
  const commit = async () => {
    const input = tagsUpdateInput(site, draft);
    setOpen(false);
    if (!input) return;
    setSaving(true);
    await onUpdate(input);
    setSaving(false);
  };

  useDismissable(open, rootRef, triggerRef, (reason) => {
    if (reason === "escape") setOpen(false);
    else void commit();
  });

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const known: LibraryTagRef[] = state.status === "ready" ? state.items : [];
  const allTags = [...known, ...extraTags.filter((tag) => !known.some((item) => item.id === tag.id))];
  const selected = new Set(draft);
  const options = filterTagOptions(allTags, query, selected);
  const candidate = tagCreateCandidate(allTags, query);

  const createTag = async () => {
    if (!candidate || creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      const result = await createLibraryTagResolvingConflict(candidate);
      if (result.latestTags) setState({ status: "ready", items: result.latestTags });
      else setExtraTags((current) => [...current, result.tag]);
      setDraft((current) => (current.includes(result.tag.id) ? current : [...current, result.tag.id]));
      setQuery("");
    } catch (error) {
      setCreateError(errorText(error, "标签没有建成，请稍后重试"));
    } finally {
      setCreating(false);
      inputRef.current?.focus();
    }
  };

  return (
    <div className="sd-picker" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="sd-picker-trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        disabled={saving}
        onClick={() => (open ? void commit() : openPicker())}
      >
        <Tag aria-hidden="true" />
        <span>编辑标签</span>
        {saving ? <Spinner size={13} /> : <ChevronDown aria-hidden="true" />}
      </button>
      {open && (
        <div className="sd-popover sd-popover-wide" role="dialog" aria-label="编辑标签" data-shortcut-block>
          <label className="sd-popover-search">
            <Search aria-hidden="true" />
            <span className="sr-only">查找或新建标签</span>
            <input
              ref={inputRef}
              type="search"
              value={query}
              placeholder="查找或新建标签"
              aria-controls={listId}
              onChange={(event) => {
                setQuery(event.target.value);
                setCreateError(null);
              }}
              onKeyDown={(event) => {
                if (event.key !== "Enter") return;
                event.preventDefault();
                const exact = exactTagOption(allTags, query);
                if (exact) setDraft((current) => toggleTagSelection(current, exact.id));
                else if (candidate) void createTag();
                else if (!query.trim()) void commit();
              }}
            />
          </label>
          {state.status === "error" ? (
            <div className="sd-popover-note" role="alert">
              {state.message}
              <button type="button" className="sd-text-button" onClick={retry}>
                <RefreshCw aria-hidden="true" />重试
              </button>
            </div>
          ) : state.status !== "ready" ? (
            <p className="sd-popover-note" role="status"><Spinner size={13} />正在读取标签…</p>
          ) : (
            <ul className="sd-popover-list" id={listId} aria-label="标签">
              {options.map((tag) => (
                <li key={tag.id}>
                  <label className="sd-popover-option">
                    <input
                      type="checkbox"
                      checked={selected.has(tag.id)}
                      onChange={() => setDraft((current) => toggleTagSelection(current, tag.id))}
                    />
                    <span className="sd-popover-label">{tag.name}</span>
                    {"siteCount" in tag && <small>{(tag as LibraryTag).siteCount}</small>}
                  </label>
                </li>
              ))}
              {options.length === 0 && !candidate && (
                <li className="sd-popover-note">没有匹配的标签</li>
              )}
            </ul>
          )}
          {candidate && state.status === "ready" && (
            <button
              type="button"
              className="sd-popover-create"
              disabled={creating}
              onClick={() => void createTag()}
            >
              {creating ? <Spinner size={13} /> : <Plus aria-hidden="true" />}
              新建标签「{candidate}」
            </button>
          )}
          {createError && <p className="sd-popover-note" role="alert">{createError}</p>}
          <div className="sd-popover-footer">
            <span>已选 {draft.length} 个 · Esc 放弃</span>
            <button type="button" className="sd-btn sd-btn-primary sd-btn-small" onClick={() => void commit()}>
              完成
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function TaxonomyEditor({ site, onUpdate }: Readonly<TaxonomyEditorProps>) {
  return (
    <section className="sd-card sd-section" aria-labelledby="sd-taxonomy-title">
      <h2 id="sd-taxonomy-title" className="sd-section-title">分类与标签</h2>
      <div className="sd-taxonomy">
        <div className="sd-taxonomy-row">
          <span className="sd-meta-label">分类</span>
          <div className="sd-taxonomy-value">
            <CategoryPicker site={site} onUpdate={onUpdate} />
          </div>
        </div>
        <div className="sd-taxonomy-row">
          <span className="sd-meta-label">标签</span>
          <div className="sd-taxonomy-value">
            {site.tags.length > 0 ? (
              <ul className="sd-chip-row" aria-label="当前标签">
                {site.tags.map((tag) => (
                  <li key={tag.id}>
                    <Link
                      className="sd-chip sd-chip-outline"
                      href={`/library?tag=${encodeURIComponent(tag.id)}`}
                      title={`在收藏库里查看「${tag.name}」`}
                    >
                      #{tag.name}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <span className="sd-meta-empty">暂无标签</span>
            )}
            <TagPicker site={site} onUpdate={onUpdate} />
          </div>
        </div>
      </div>
    </section>
  );
}
