// 详情页仓库面板（Q45）的纯计算：把后端写在 meta.github 下的仓库数据
// （services/api/src/webhub/ingestion/github.py）整理成面板要显示的几项。
// 组件 components/library/detail/repo-panel-card.tsx 只负责画。

import type { LibrarySiteMeta } from "./library-contract.ts";
import { formatStars, githubRepoMeta } from "./resource-cover.ts";

const DAY_MS = 86_400_000;

/** “N 天前推送”：不足一天为“今天推送”，30 天起按月、365 天起按年；未来时间（时钟偏差）按今天算。 */
export function pushedAgoLabel(pushedAt: string, now: number = Date.now()): string | null {
  const time = Date.parse(pushedAt);
  if (Number.isNaN(time)) return null;
  const days = Math.max(0, Math.floor((now - time) / DAY_MS));
  if (days === 0) return "今天推送";
  if (days < 30) return `${days} 天前推送`;
  if (days < 365) return `${Math.floor(days / 30)} 个月前推送`;
  return `${Math.floor(days / 365)} 年前推送`;
}

export type RepoPanelModel = {
  stars: string | null;
  forks: string | null;
  language: string | null;
  topics: string[];
  license: string | null;
  pushed: string | null;
  archived: boolean;
  /** 没有数字可显示时说明原因（仓库不存在 / 被限流）；有数据时为 null。 */
  notice: string | null;
};

export const REPO_NOT_FOUND_NOTICE = "GitHub 上找不到这个仓库：可能已删除、改名或设为私有。";
export const REPO_RATE_LIMITED_NOTICE = "GitHub 接口暂时限流，稍后重新分析即可读取仓库数据。";

/** 没有 meta.github，或其中既没有数据也没有可说明的状态时返回 null（面板不显示）。 */
export function repoPanelModel(meta: LibrarySiteMeta, now: number = Date.now()): RepoPanelModel | null {
  const github = githubRepoMeta(meta);
  if (!github) return null;
  const empty: RepoPanelModel = {
    stars: null,
    forks: null,
    language: null,
    topics: [],
    license: null,
    pushed: null,
    archived: false,
    notice: null,
  };
  if (github.notFound) return { ...empty, notice: REPO_NOT_FOUND_NOTICE };
  if (github.stars === null && github.forks === null) {
    return github.rateLimitedAt ? { ...empty, notice: REPO_RATE_LIMITED_NOTICE } : null;
  }
  return {
    stars: github.stars === null ? null : formatStars(github.stars),
    forks: github.forks === null ? null : formatStars(github.forks),
    language: github.language,
    topics: github.topics,
    license: github.license,
    pushed: github.pushedAt ? pushedAgoLabel(github.pushedAt, now) : null,
    archived: github.archived,
    notice: null,
  };
}
