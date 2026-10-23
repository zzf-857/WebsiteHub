// 订阅条目左侧的来源标识：订阅源封面、站点图标（有 site_url 时）或字母块。
// 字母块的色相与封面共用同一个确定性哈希（resource-cover 的 siteHue），颜色由 CSS 令牌按 --site-hue 派生。
// 纯函数，Node 测试直接覆盖。

import { hostLabel, type Feed } from "./feeds-contract.ts";
import { proxiedImageUrl } from "./media-proxy.ts";
import { siteHue } from "./resource-cover.ts";

export type FeedMark = {
  /** 字母块上的首字；没有来源信息时为 “?”。 */
  letter: string;
  /** 0–359，作为 CSS 变量 --site-hue 挂到标识上。 */
  hue: number;
  /** 站点自己的 /favicon.ico（同源地址，不走第三方 CDN）；没有 site_url 时为 null。 */
  faviconUrl: string | null;
  /** 订阅源的频道封面，优先于站点图标。 */
  imageUrl: string | null;
};

type MarkSource = Pick<Feed, "title" | "url" | "siteUrl"> & Partial<Pick<Feed, "imageUrl">>;

export type FeedMarkSource =
  | { kind: "image" | "favicon"; url: string }
  | { kind: "podcast" | "letter" };

function siteOrigin(siteUrl: string | null): string | null {
  if (!siteUrl) return null;
  try {
    const url = new URL(siteUrl);
    return url.protocol === "https:" || url.protocol === "http:" ? url.origin : null;
  } catch {
    return null;
  }
}

/** feed 缺失（来源已被删除）时用 fallbackTitle 兜底，保证永远有字母与色相。 */
export function feedMark(feed: MarkSource | null | undefined, fallbackTitle = ""): FeedMark {
  const title = feed?.title.trim() || fallbackTitle.trim();
  const origin = siteOrigin(feed?.siteUrl ?? null);
  const hueKey = hostLabel(feed?.siteUrl ?? null) || hostLabel(feed?.url ?? null) || title;
  return {
    letter: (Array.from(title)[0] ?? "?").toLocaleUpperCase(),
    hue: siteHue(hueKey),
    faviconUrl: origin ? `${origin}/favicon.ico` : null,
    imageUrl: feed?.imageUrl ?? null,
  };
}

/** 每次图片失败后重新选择下一候选；播客只在封面缺失或失败时显示耳机块。 */
export function feedMarkSource(
  mark: FeedMark,
  podcast: boolean,
  failedSources: ReadonlySet<string> = new Set(),
): FeedMarkSource {
  const image = proxiedImageUrl(mark.imageUrl);
  if (image && !failedSources.has(image)) return { kind: "image", url: image };
  if (podcast) return { kind: "podcast" };
  const favicon = proxiedImageUrl(mark.faviconUrl);
  if (favicon && !failedSources.has(favicon)) return { kind: "favicon", url: favicon };
  return { kind: "letter" };
}
