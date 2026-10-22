import type { LibrarySite } from "@/lib/library-contract";
import { formatRelativeTime, parseServerTime } from "@/lib/relative-time";

import { ANALYSIS_STATUS_LABELS, formatDay, SOURCE_LABELS } from "./meta";

// 使用记录（Q46）：打开次数、最近打开、重温（推迟）次数，以及收藏与整理的时间线。
// 全部来自站点自身的真实字段；没有发生过的事情写“从未”，不编造。

type UsageCardProps = {
  site: LibrarySite;
  now: number;
};

function UsageTime({ value, now }: Readonly<{ value: string; now: number }>) {
  return (
    <time dateTime={value} title={formatDay(value)}>
      {formatRelativeTime(value, now)}
    </time>
  );
}

export function UsageCard({ site, now }: Readonly<UsageCardProps>) {
  const dueAt = site.resurfaceDueAt;
  const snoozed = dueAt !== null && parseServerTime(dueAt) > now;

  return (
    <section className="sd-card sd-section" aria-labelledby="sd-usage-title">
      <h2 id="sd-usage-title" className="sd-section-title">使用记录</h2>
      <dl className="sd-usage-stats">
        <div>
          <dt>打开次数</dt>
          <dd>{site.openCount} 次</dd>
        </div>
        <div>
          <dt>最近打开</dt>
          <dd>{site.lastOpenedAt ? <UsageTime value={site.lastOpenedAt} now={now} /> : "从未打开"}</dd>
        </div>
        <div>
          <dt>重温次数</dt>
          <dd title="在重温里选择“下周再提醒”的次数">{site.resurfaceCount} 次</dd>
        </div>
      </dl>
      <ul className="sd-usage-list">
        <li>
          <span>阅读状态</span>
          <span>
            {site.seenAt ? (
              <>已看过 · <UsageTime value={site.seenAt} now={now} /></>
            ) : (
              "还没看过"
            )}
          </span>
        </li>
        <li>
          <span>重温</span>
          <span>
            {site.resurfaceMuted
              ? "已设为不再推荐"
              : snoozed && dueAt
                ? <>推迟到 <time dateTime={dueAt}>{formatDay(dueAt)}</time></>
                : "按遗忘曲线自动安排"}
          </span>
        </li>
        <li>
          <span>收藏于</span>
          <span>
            <time dateTime={site.createdAt}>{formatDay(site.createdAt)}</time> · {SOURCE_LABELS[site.source]}
          </span>
        </li>
        <li>
          <span>最近整理</span>
          <span>
            <time dateTime={site.updatedAt}>{formatDay(site.updatedAt)}</time> · {ANALYSIS_STATUS_LABELS[site.analysisStatus]}
          </span>
        </li>
      </ul>
    </section>
  );
}
