"use client";

// 从 agent-panel.tsx 拆出来的部分。拆分依据不是"文件太长"，而是这里的东西
// **不持有任何组件状态**：纯函数、本地类型，以及只吃 props 的展示组件。
// 和 800 行的有状态主组件混在一起时，读的人得先确认它们没闭包捕获才敢改；
// 放在独立模块里，这个疑问一开始就不存在。

import {
  ArrowUpRight,
  ChevronRight,
  Plus,
} from "lucide-react";
import Link from "next/link";
import {
  PopCheck,
} from "@/components/react-bits/pop-check";
import {
  Spinner,
} from "@/components/react-bits/spinner";
import {
  SiteFavicon,
} from "@/components/site-favicon";
import {
  type AgentToolLink,
} from "@/lib/agent-contract";

export type SearchScope = "collection" | "online";

// 阶段进度条的每一步都由真实工具事件驱动：call 事件入场，result 事件定稿
export type AgentStage = {
  id: string;
  tool: "search_library" | "web_search";
  status: "active" | "done";
  count: number | null;
  provider: string | null;
};

export type WebSaveState = {
  status: "idle" | "saving" | "saved" | "error";
  message?: string;
};

export const IDLE_SAVE: WebSaveState = { status: "idle" };

export type ResultGroups = {
  library: { items: AgentToolLink[]; total: number };
  web: { items: AgentToolLink[]; provider: string | null };
};

export function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function cardKey(link: AgentToolLink, index: number): string {
  return link.siteId ?? link.url ?? `${link.name}-${index}`;
}

// 后端 web_search 结果目前不带 provider 字段；这里防御性读取，
// 一旦后端补上就自动显示真实名称，绝不硬编码某家搜索商
export function readWebProvider(result: unknown): string | null {
  if (typeof result !== "object" || result === null || Array.isArray(result)) return null;
  const provider = (result as Record<string, unknown>).provider;
  return typeof provider === "string" && provider.trim() ? provider.trim() : null;
}

// 会话标题旁的时间：同天说"今天"，跨天给日期，避免展示裸 ISO 字符串
export function formatConversationTime(iso: string): string | null {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const time = date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const now = new Date();
  const dayStart = (value: Date) =>
    new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  const diffDays = Math.round((dayStart(now) - dayStart(date)) / 86_400_000);
  if (diffDays === 0) return `今天 ${time}`;
  if (diffDays === 1) return `昨天 ${time}`;
  if (date.getFullYear() === now.getFullYear()) {
    return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
  }
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
}

/* ---------- 结果卡片（设计稿 1b 的两类卡） ---------- */

export function LibraryResultCard({ link }: Readonly<{ link: AgentToolLink }>) {
  return (
    <article className="agent-site-card">
      <SiteFavicon url={link.faviconUrl} name={link.name} size={28} />
      <div className="agent-site-main">
        <div className="agent-site-title">
          <strong>{link.name}</strong>
          {link.url && <span>{hostOf(link.url)}</span>}
        </div>
        {link.description && <p className="agent-site-desc">{link.description}</p>}
        <div className="agent-site-meta">
          <span className="agent-site-origin">
            收藏库{link.category ? ` · ${link.category}` : ""}
          </span>
          {link.pinned && <span>已置顶</span>}
        </div>
      </div>
      {link.siteId && (
        <Link
          className="agent-open-button"
          href={`/library/${encodeURIComponent(link.siteId)}`}
        >
          详情
          <ChevronRight aria-hidden="true" />
        </Link>
      )}
    </article>
  );
}

export function WebResultCard({
  link,
  providerLabel,
  state,
  onCollect,
}: Readonly<{
  link: AgentToolLink;
  providerLabel: string;
  state: WebSaveState;
  onCollect: (link: AgentToolLink) => void;
}>) {
  const saved = state.status === "saved";
  return (
    <article className="agent-site-card" data-external="true">
      {/* 28×28 对齐设计稿行 231 */}
      <SiteFavicon url={link.faviconUrl} name={link.name} size={28} />
      <div className="agent-site-main">
        <div className="agent-site-title">
          <strong>{link.name}</strong>
          {link.url && <span>{hostOf(link.url)}</span>}
        </div>
        {link.description && <p className="agent-site-desc">{link.description}</p>}
        <div className="agent-site-meta">
          <span className="agent-site-origin" data-tone="accent">
            来源 · {providerLabel}
          </span>
          <span>{saved ? "已收录" : "未收录"}</span>
        </div>
        {state.status === "error" && state.message && (
          <p className="agent-site-error" role="alert">
            {state.message}
          </p>
        )}
      </div>
      <div className="agent-site-actions">
        {link.url && (
          <button
            type="button"
            className="agent-collect-button"
            data-done={saved || undefined}
            disabled={saved || state.status === "saving"}
            onClick={() => onCollect(link)}
          >
            {saved ? (
              <>
                <PopCheck done size={13} />
                已收录
              </>
            ) : state.status === "saving" ? (
              <>
                <Spinner size={12} />
                收录中…
              </>
            ) : (
              <>
                <Plus aria-hidden="true" />
                收录
              </>
            )}
          </button>
        )}
        {link.url && (
          <a
            className="agent-open-button"
            href={link.url}
            target="_blank"
            rel="noreferrer noopener"
          >
            打开
            <ArrowUpRight aria-hidden="true" />
          </a>
        )}
      </div>
    </article>
  );
}

/* ---------- 首页内嵌 Agent 模块 ---------- */
