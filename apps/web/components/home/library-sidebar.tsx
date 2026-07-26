"use client";

import {
  BookOpen,
  Box,
  Code,
  Coffee,
  Folder,
  Inbox,
  LayoutGrid,
  Palette,
  Plus,
  Sparkles,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { listLibraryCategories } from "@/lib/library-client";
import type { LibraryCategory } from "@/lib/library-contract";
import { listSpaces } from "@/lib/space-client";
import type { Space } from "@/lib/space-contract";

/* 分类名 → 1f 规范板约定的 lucide 图标；未约定的分类统一回落到 Folder，
   避免后端新增分类时前端出现无图标的空洞。 */
const CATEGORY_ICONS: Record<string, typeof Folder> = {
  开发: Code,
  "AI 工具": Sparkles,
  学习: BookOpen,
  设计: Palette,
  日常: Coffee,
  未分类: Inbox,
};

/* 骨架条数量取自设计稿 hint-placeholder-count，与真实数据的常见规模一致。 */
const CATEGORY_SKELETON_COUNT = 6;
const SPACE_SKELETON_COUNT = 4;

type SectionStatus = "loading" | "error" | "ready";

type LibrarySidebarProps = {
  activeCategoryId: string | null;
  onSelectCategory: (id: string | null) => void;
};

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function LibrarySidebar({ activeCategoryId, onSelectCategory }: LibrarySidebarProps) {
  const router = useRouter();

  const [categories, setCategories] = useState<LibraryCategory[]>([]);
  const [categoryStatus, setCategoryStatus] = useState<SectionStatus>("loading");
  const [categoryError, setCategoryError] = useState<string | null>(null);

  const [spaces, setSpaces] = useState<Space[]>([]);
  const [spaceStatus, setSpaceStatus] = useState<SectionStatus>("loading");
  const [spaceError, setSpaceError] = useState<string | null>(null);

  const loadCategories = useCallback(async (signal?: AbortSignal) => {
    setCategoryStatus("loading");
    setCategoryError(null);
    try {
      const items = await listLibraryCategories(signal);
      if (signal?.aborted) return;
      setCategories(items);
      setCategoryStatus("ready");
    } catch (error) {
      if (isAbortError(error) || signal?.aborted) return;
      setCategoryError(errorMessage(error, "分类加载失败，请重试"));
      setCategoryStatus("error");
    }
  }, []);

  const loadSpaces = useCallback(async (signal?: AbortSignal) => {
    setSpaceStatus("loading");
    setSpaceError(null);
    try {
      const page = await listSpaces({}, signal);
      if (signal?.aborted) return;
      setSpaces(page.items);
      setSpaceStatus("ready");
    } catch (error) {
      if (isAbortError(error) || signal?.aborted) return;
      setSpaceError(errorMessage(error, "Space 加载失败，请重试"));
      setSpaceStatus("error");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    /* 挪到微任务里执行：加载函数会同步 setState（置为 loading），
       直接在 effect 体内调用会触发 react-hooks/set-state-in-effect。 */
    void Promise.resolve().then(() => {
      void loadCategories(controller.signal);
      void loadSpaces(controller.signal);
    });
    return () => controller.abort();
  }, [loadCategories, loadSpaces]);

  /* “全部”的计数是各分类 siteCount 之和，与设计稿 1a 保持一致。 */
  const totalCount = categories.reduce((sum, category) => sum + category.siteCount, 0);

  return (
    <nav className="library-sidebar" aria-label="分类与 Space">
      <button
        type="button"
        className="library-sidebar-item"
        data-active={activeCategoryId === null || undefined}
        aria-current={activeCategoryId === null ? "true" : undefined}
        onClick={() => onSelectCategory(null)}
      >
        <LayoutGrid className="library-sidebar-icon" aria-hidden="true" />
        <span className="library-sidebar-label">全部</span>
        {categoryStatus === "ready" && (
          <span className="library-sidebar-count">{totalCount}</span>
        )}
      </button>

      {categoryStatus === "loading" &&
        Array.from({ length: CATEGORY_SKELETON_COUNT }, (_, index) => (
          <div className="library-sidebar-skeleton" key={index} aria-hidden="true" />
        ))}

      {categoryStatus === "error" && (
        <div className="library-sidebar-error" role="alert">
          <span>{categoryError}</span>
          <button type="button" onClick={() => void loadCategories()}>
            重试
          </button>
        </div>
      )}

      {categoryStatus === "ready" && categories.length === 0 && (
        <p className="library-sidebar-empty">暂无分类</p>
      )}

      {categoryStatus === "ready" &&
        categories.map((category) => {
          const Icon = CATEGORY_ICONS[category.name] ?? Folder;
          const isActive = category.id === activeCategoryId;
          return (
            <button
              type="button"
              className="library-sidebar-item"
              data-active={isActive || undefined}
              aria-current={isActive ? "true" : undefined}
              key={category.id}
              onClick={() => onSelectCategory(category.id)}
            >
              <Icon className="library-sidebar-icon" aria-hidden="true" />
              <span className="library-sidebar-label">{category.name}</span>
              <span className="library-sidebar-count">{category.siteCount}</span>
            </button>
          );
        })}

      <div className="library-sidebar-divider" aria-hidden="true" />

      <div className="library-sidebar-heading">SPACE</div>

      {spaceStatus === "loading" &&
        Array.from({ length: SPACE_SKELETON_COUNT }, (_, index) => (
          <div
            className="library-sidebar-skeleton library-sidebar-skeleton--space"
            key={index}
            aria-hidden="true"
          />
        ))}

      {spaceStatus === "error" && (
        <div className="library-sidebar-error" role="alert">
          <span>{spaceError}</span>
          <button type="button" onClick={() => void loadSpaces()}>
            重试
          </button>
        </div>
      )}

      {spaceStatus === "ready" && spaces.length === 0 && (
        <p className="library-sidebar-empty">暂无 Space</p>
      )}

      {spaceStatus === "ready" &&
        spaces.map((space) => (
          <button
            type="button"
            className="library-sidebar-item library-sidebar-item--space"
            key={space.id}
            onClick={() => router.push(`/spaces/${space.id}`)}
          >
            <Box className="library-sidebar-icon" aria-hidden="true" />
            <span className="library-sidebar-label">{space.name}</span>
            <span className="library-sidebar-count">{space.memberCount}</span>
          </button>
        ))}

      <button
        type="button"
        className="library-sidebar-new-space"
        onClick={() => router.push("/spaces")}
      >
        <Plus className="library-sidebar-icon" aria-hidden="true" />
        <span>新建 Space</span>
      </button>
    </nav>
  );
}
