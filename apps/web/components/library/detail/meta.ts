import type { LibraryAnalysisStatus, LibrarySiteSource } from "@/lib/library-contract";

/* 详情页共用的文案映射与时间格式化。
   全部基于后端真实字段（source / analysisStatus / createdAt / updatedAt），
   不引入后端没有的概念。 */

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
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** 收录动态里的相对时间（「3 天前」）；未来时间（时钟偏差）统一按「刚刚」处理 */
export function formatRelative(value: string): string {
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return "";
  const diff = Date.now() - time;
  const minute = 60_000;
  const hour = 3_600_000;
  const day = 86_400_000;
  if (diff < minute) return "刚刚";
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < day) return `${Math.floor(diff / hour)} 小时前`;
  if (diff < day * 30) return `${Math.floor(diff / day)} 天前`;
  if (diff < day * 365) return `${Math.floor(diff / (day * 30))} 个月前`;
  return `${Math.floor(diff / (day * 365))} 年前`;
}

/** 从 URL 提取主机名做展示；解析失败时原样返回，避免吞掉信息 */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}
