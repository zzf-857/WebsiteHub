import { Check, CircleAlert, History, Pencil, Plus, Sparkles } from "lucide-react";
import type { ReactNode } from "react";

import { Spinner } from "@/components/react-bits/spinner";
import type { LibrarySite, LibrarySiteSource } from "@/lib/library-contract";

import { formatDay, formatRelative } from "./meta";

/* 取舍说明（设计稿 1d「收录动态」）：后端没有独立的活动流/审计表，
   这里只用站点自身四个真实字段（createdAt / updatedAt / source / analysisStatus）
   组合出可以证实的记录；「加入了某个 Space」这类没有数据支撑的条目一律不虚构。 */

type ActivityEntry = {
  key: string;
  /** accent 表示与 Agent/确认相关的正向动作，muted 是普通记录 */
  tone: "accent" | "muted";
  icon: ReactNode;
  text: string;
  meta: string;
};

const CREATED_BY_SOURCE: Record<LibrarySiteSource, { tone: "accent" | "muted"; icon: ReactNode; text: string }> = {
  agent: { tone: "accent", icon: <Sparkles size={16} aria-hidden="true" />, text: "由 Agent 收录到网址库" },
  manual: { tone: "muted", icon: <Plus size={16} aria-hidden="true" />, text: "手动添加到网址库" },
  browser_import: { tone: "muted", icon: <History size={16} aria-hidden="true" />, text: "从浏览器书签导入" },
  backup: { tone: "muted", icon: <History size={16} aria-hidden="true" />, text: "从备份恢复" },
};

function buildEntries(site: LibrarySite): ActivityEntry[] {
  const entries: ActivityEntry[] = [];

  // 时间倒序：最新的在最上面（与设计稿一致）
  if (site.updatedAt !== site.createdAt) {
    entries.push({
      key: "updated",
      tone: "accent",
      icon: <Pencil size={16} aria-hidden="true" />,
      text: "收录信息有更新",
      meta: `${formatDay(site.updatedAt)} · ${formatRelative(site.updatedAt)}`,
    });
  }

  // 后端没有记录分析发生的具体时间，右侧只展示状态词，不编造时间戳
  if (site.analysisStatus !== "not_analyzed") {
    const byStatus = {
      pending: {
        tone: "muted",
        icon: <Spinner size={16} />,
        text: "正在分析网站资料",
        meta: "进行中",
      },
      complete: {
        tone: "accent",
        icon: <Check size={16} aria-hidden="true" />,
        text: "网站资料分析已完成",
        meta: "已完成",
      },
      failed: {
        tone: "muted",
        icon: <CircleAlert size={16} aria-hidden="true" />,
        text: "网站资料分析未成功",
        meta: "失败",
      },
      limited: {
        tone: "muted",
        icon: <CircleAlert size={16} aria-hidden="true" />,
        text: "网站资料仅完成部分分析",
        meta: "部分完成",
      },
    } as const;
    entries.push({ key: "analysis", ...byStatus[site.analysisStatus] });
  }

  entries.push({
    key: "created",
    ...CREATED_BY_SOURCE[site.source],
    meta: `${formatDay(site.createdAt)} · ${formatRelative(site.createdAt)}`,
  });

  return entries;
}

type ActivityCardProps = {
  site: LibrarySite;
};

export function ActivityCard({ site }: Readonly<ActivityCardProps>) {
  const entries = buildEntries(site);

  return (
    <section className="sd-card sd-section" aria-labelledby="sd-activity-title">
      <h2 id="sd-activity-title" className="sd-section-title">
        收录动态
      </h2>
      <ul className="sd-activity-list">
        {entries.map((entry) => (
          <li key={entry.key} className="sd-activity-item" data-tone={entry.tone}>
            {entry.icon}
            <span className="sd-activity-text">{entry.text}</span>
            <span className="sd-activity-spacer" aria-hidden="true" />
            <span className="sd-activity-meta">{entry.meta}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
