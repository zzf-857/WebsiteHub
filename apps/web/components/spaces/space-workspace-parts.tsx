"use client";

// 从 space-workspace.tsx 拆出来的部分。拆分依据不是"文件太长"，而是这里的东西
// **不持有任何组件状态**：纯函数、本地类型，以及只吃 props 的展示组件。
// 和 800 行的有状态主组件混在一起时，读的人得先确认它们没闭包捕获才敢改；
// 放在独立模块里，这个疑问一开始就不存在。

import {
  ArrowDown,
  ArrowUp,
  Blocks,
  CalendarDays,
  ChevronsDown,
  ChevronsUp,
  ExternalLink,
  FolderOpen,
  GripVertical,
  LoaderCircle,
  Pencil,
  Trash2,
  Users,
} from "lucide-react";
import Link from "next/link";
import {
  useState,
  type FormEvent,
} from "react";
import {
  SiteFavicon,
} from "@/components/site-favicon";
import {
  SpaceApiError,
} from "@/lib/space-client";
import {
  MAX_SPACE_NAME_LENGTH,
  type Space,
  type SpaceDeletePreview,
  type SpaceMember,
} from "@/lib/space-contract";

export type SpacePageState = {
  items: Space[];
  nextCursor: string | null;
  totalCount: number;
  loadingMore: boolean;
};

export type DialogState =
  | { kind: "create" }
  | { kind: "rename"; space: Space }
  | {
    kind: "delete";
    space: Space;
    preview: SpaceDeletePreview | null;
    previewLoading: boolean;
  }
  | { kind: "remove"; member: SpaceMember }
  | null;

export type MemberMove = "top" | "up" | "down" | "bottom";

export const EMPTY_SPACE_PAGE: SpacePageState = {
  items: [],
  nextCursor: null,
  totalCount: 0,
  loadingMore: false,
};

export function appendUniqueSpaces(current: Space[], incoming: Space[]): Space[] {
  const ids = new Set(current.map((space) => space.id));
  return [...current, ...incoming.filter((space) => !ids.has(space.id))];
}

export function appendUniqueMembers(current: SpaceMember[], incoming: SpaceMember[]): SpaceMember[] {
  const ids = new Set(current.map((member) => member.site.id));
  return [...current, ...incoming.filter((member) => !ids.has(member.site.id))];
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function isSpaceStatus(error: unknown, status: number): error is SpaceApiError {
  return error instanceof SpaceApiError && error.status === status;
}

export function isSpaceCode(error: unknown, code: string): error is SpaceApiError {
  return error instanceof SpaceApiError && error.code === code;
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

export function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(new Date(value));
}

export function siteHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function SpaceNameForm({
  initialName = "",
  busy,
  error,
  submitLabel,
  onCancel,
  onSubmit,
}: Readonly<{
  initialName?: string;
  busy: boolean;
  error: string | null;
  submitLabel: string;
  onCancel: () => void;
  onSubmit: (name: string) => Promise<void>;
}>) {
  const [name, setName] = useState(initialName);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void onSubmit(name);
  };

  return (
    <form className="space-form" onSubmit={handleSubmit}>
      <label className="space-field">
        <span>Space 名称</span>
        <input
          autoFocus
          required
          maxLength={MAX_SPACE_NAME_LENGTH}
          value={name}
          disabled={busy}
          placeholder="例如：产品调研"
          onChange={(event) => setName(event.target.value)}
        />
        <small>{Array.from(name).length} / {MAX_SPACE_NAME_LENGTH}</small>
      </label>
      {error && <p className="space-form-error" role="alert">{error}</p>}
      <div className="space-form-actions">
        <button className="space-button secondary" type="button" disabled={busy} onClick={onCancel}>
          取消
        </button>
        <button className="space-button primary" type="submit" disabled={busy || !name.trim()}>
          {busy && <LoaderCircle className="loading-spinner" aria-hidden="true" />}
          {busy ? "正在保存" : submitLabel}
        </button>
      </div>
    </form>
  );
}

export function SpaceCard({
  space,
  onOpen,
  onOpenAll,
  onRename,
  onDelete,
}: Readonly<{
  space: Space;
  onOpen: () => void;
  onOpenAll: () => void;
  onRename: () => void;
  onDelete: () => void;
}>) {
  return (
    <article className="space-card">
      <div className="space-card-icon" aria-hidden="true">
        <Blocks />
      </div>
      <div className="space-card-copy">
        <button className="space-card-title" type="button" onClick={onOpen}>
          {space.name}
        </button>
        <p><Users aria-hidden="true" />{space.memberCount} 个网站</p>
      </div>
      <div className="space-card-meta">
        <span><CalendarDays aria-hidden="true" />更新于 {formatDate(space.updatedAt)}</span>
        <div className="space-card-actions">
          <button className="icon-button" type="button" onClick={onRename} aria-label={`重命名 ${space.name}`} title="重命名">
            <Pencil aria-hidden="true" />
          </button>
          <button className="icon-button space-danger-action" type="button" onClick={onDelete} aria-label={`删除 ${space.name}`} title="删除">
            <Trash2 aria-hidden="true" />
          </button>
          <button
            className="space-button secondary"
            type="button"
            disabled={space.memberCount === 0}
            onClick={onOpenAll}
            title={space.memberCount === 0 ? "这个 Space 还没有网站" : "一次打开其中全部网站"}
          >
            <ExternalLink aria-hidden="true" />
            全部打开
          </button>
          <button className="space-button secondary" type="button" onClick={onOpen}>
            <FolderOpen aria-hidden="true" />
            打开
          </button>
        </div>
      </div>
    </article>
  );
}

export function SpaceMemberRow({
  member,
  order,
  totalCount,
  busy,
  onMove,
  onRemove,
}: Readonly<{
  member: SpaceMember;
  order: number;
  totalCount: number;
  busy: boolean;
  onMove: (direction: MemberMove) => void;
  onRemove: () => void;
}>) {
  const isFirst = order === 0;
  const isLast = order >= totalCount - 1;

  return (
    <li className="space-member-row" aria-busy={busy || undefined}>
      <span className="space-member-position" title={`第 ${order + 1} 位`}>
        <GripVertical aria-hidden="true" />
        <span>{order + 1}</span>
      </span>
      <SiteFavicon url={member.site.faviconUrl} name={member.site.name} size={24} />
      <div className="space-member-copy">
        <div className="space-member-title-row">
          <Link href={`/library/${encodeURIComponent(member.site.id)}`}>{member.site.name}</Link>
          {member.site.pinned && <span className="space-pinned-label">已置顶</span>}
        </div>
        <a
          className="space-member-host"
          href={member.site.originalUrl}
          target="_blank"
          rel="noopener noreferrer"
          referrerPolicy="no-referrer"
          title={member.site.originalUrl}
        >
          {siteHost(member.site.originalUrl)}
          <ExternalLink aria-hidden="true" />
        </a>
        {member.site.description && <p>{member.site.description}</p>}
      </div>
      <div className="space-member-actions" role="group" aria-label={`调整 ${member.site.name} 的顺序`}>
        <button className="icon-button" type="button" disabled={busy || isFirst} onClick={() => onMove("top")} aria-label="移到最前" title="移到最前">
          <ChevronsUp aria-hidden="true" />
        </button>
        <button className="icon-button" type="button" disabled={busy || isFirst} onClick={() => onMove("up")} aria-label="上移" title="上移">
          <ArrowUp aria-hidden="true" />
        </button>
        <button className="icon-button" type="button" disabled={busy || isLast} onClick={() => onMove("down")} aria-label="下移" title="下移">
          <ArrowDown aria-hidden="true" />
        </button>
        <button className="icon-button" type="button" disabled={busy || isLast} onClick={() => onMove("bottom")} aria-label="移到最后" title="移到最后">
          <ChevronsDown aria-hidden="true" />
        </button>
        <button className="icon-button space-danger-action" type="button" disabled={busy} onClick={onRemove} aria-label={`从 Space 移除 ${member.site.name}`} title="移除">
          <Trash2 aria-hidden="true" />
        </button>
      </div>
    </li>
  );
}
