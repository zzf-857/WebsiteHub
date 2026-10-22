import type { LibraryAnalysisStatus, LibrarySiteSource } from "@/lib/library-contract";
import { parseServerTime } from "@/lib/relative-time";

/* 详情页共用的文案映射与日期格式化。
   全部基于后端真实字段（source / analysisStatus / createdAt / updatedAt），
   不引入后端没有的概念。相对时间统一用 lib/relative-time。 */

export const SOURCE_LABELS: Record<LibrarySiteSource, string> = {
  manual: "手动添加",
  agent: "Agent 收录",
  browser_import: "浏览器导入",
  backup: "备份恢复",
};

export const ANALYSIS_STATUS_LABELS: Record<LibraryAnalysisStatus, string> = {
  not_analyzed: "未分析",
  pending: "分析中",
  complete: "分析完成",
  failed: "分析失败",
  limited: "部分完成",
};

/** 设计稿 1d 的日期只精确到天且用短横线（如「添加于 2025-11-03」）。
    不用 Intl：zh-CN locale 输出的是「2025/11/03」斜杠格式，与设计稿不符，
    所以这里自己拼 YYYY-MM-DD。 */
export function formatDay(value: string): string {
  // 后端时间不带时区偏移、语义上是 UTC（Q42）；按 UTC 解析后再以本地日期展示
  const date = new Date(parseServerTime(value));
  if (Number.isNaN(date.getTime())) return value;
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** 从 URL 提取主机名做展示；解析失败时原样返回，避免吞掉信息 */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}
