// 收藏时间的相对文案（发现页资源卡元信息行）。纯函数，便于 Node 测试直接覆盖。
//
// SQLite 返回的时间不带时区偏移（Q42 实现说明），语义上是 UTC；
// 直接交给 Date.parse 会被当成本地时间，东八区会整整差出 8 小时。

const TIMEZONE_SUFFIX = /(?:z|[+-]\d{2}:?\d{2})$/iu;

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Parse an API timestamp as UTC when it carries no offset; `NaN` when invalid. */
export function parseServerTime(value: string): number {
  const trimmed = value.trim();
  if (!trimmed) return Number.NaN;
  // 纯日期（YYYY-MM-DD）本来就按 UTC 解析，只给带时刻的值补 Z
  const normalized = trimmed.includes("T") && !TIMEZONE_SUFFIX.test(trimmed)
    ? `${trimmed}Z`
    : trimmed;
  return Date.parse(normalized);
}

/**
 * 刚刚 / N 分钟前 / N 小时前 / N 天前 / N 个月前 / N 年前。
 * 时钟偏差导致的“未来时间”一律按刚刚处理，不显示负数。
 */
export function formatRelativeTime(value: string, now: number): string {
  const time = parseServerTime(value);
  if (Number.isNaN(time) || !Number.isFinite(now)) return "";
  const elapsed = now - time;
  if (elapsed < MINUTE) return "刚刚";
  if (elapsed < HOUR) return `${Math.floor(elapsed / MINUTE)} 分钟前`;
  if (elapsed < DAY) return `${Math.floor(elapsed / HOUR)} 小时前`;
  const days = Math.floor(elapsed / DAY);
  if (days < 30) return `${days} 天前`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} 个月前`;
  return `${Math.floor(days / 365)} 年前`;
}

/**
 * 重温主卡的“43 天前收藏”。按规格始终用天数（“收藏多久”本身就是重温理由，
 * 折算成“1 个月前”反而模糊）；0 天写作“今天收藏”。
 */
export function formatSavedDays(days: number): string {
  if (!Number.isFinite(days) || days < 1) return "今天收藏";
  return `${Math.floor(days)} 天前收藏`;
}
