// 资源封面的数据模型（2026-10 D4：封面不存图，渲染时由数据决定）。
// 网页自带预览图优先；否则按资源类型生成仓库 / 要点 / 视频 / 站点四种封面。
// 这里只做纯计算，组件（components/discover/resource-cover.tsx）只负责画。

import {
  libraryRepoMeta,
  type LibrarySite,
  type LibrarySiteKind,
  type LibrarySiteMeta,
} from "./library-contract.ts";

export type CoverSize = "card" | "hero" | "thumb";

type CoverBase = {
  size: CoverSize;
  /** 去掉 www. 的主机名，用于展示与色相。 */
  domain: string;
  /** 0–359，由域名确定性推出，作为 CSS 变量 --site-hue 挂到封面上。 */
  hue: number;
};

export type RepoCover = CoverBase & {
  type: "repo";
  host: string;
  owner: string;
  repo: string;
  language: string | null;
  /** 已格式化的星数（如 “1.2k”“3.4 万”），没有数据时为 null。 */
  stars: string | null;
  /** 主卡封面里仓库名下方的一句话介绍。 */
  blurb: string | null;
};

export type ArticleCover = CoverBase & { type: "article"; hook: string };
export type VideoCover = CoverBase & { type: "video" };
export type SiteCover = CoverBase & { type: "site"; name: string; faviconUrl: string | null };

export type GeneratedCover = RepoCover | ArticleCover | VideoCover | SiteCover;

export type ImageCover = CoverBase & {
  type: "image";
  src: string;
  aspect: "1.91 / 1" | "16 / 9";
  /** 图片加载失败时退回的生成封面。 */
  fallback: GeneratedCover;
};

export type CoverModel = ImageCover | GeneratedCover;

type CoverSite = Pick<
  LibrarySite,
  "name" | "originalUrl" | "summary" | "description" | "faviconUrl" | "previewUrl" | "kind" | "meta"
>;

const MAX_PREVIEW_URL_LENGTH = 2048;

/** 主机名（小写、去掉 www. 与结尾的点）；解析失败时退回原文，保证不抛异常。 */
export function siteDomain(rawUrl: string): string {
  try {
    return new URL(rawUrl).hostname.toLowerCase().replace(/\.$/u, "").replace(/^www\./u, "");
  } catch {
    return rawUrl.trim();
  }
}

/** FNV-1a 32 位哈希取模 360：同一域名永远同一色相，不同域名分布均匀。 */
export function siteHue(domain: string): number {
  let hash = 0x811c9dc5;
  for (const char of domain.trim().toLowerCase()) {
    hash ^= char.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash % 360;
}

/**
 * 只接受 http(s) 的绝对地址，拒绝 javascript:/data: 等协议、带账号密码的地址与超长地址。
 * 预览图来自第三方网页的 og:image，前端是最后一道闸。
 */
export function isSafePreviewUrl(value: string | null | undefined): value is string {
  if (typeof value !== "string") return false;
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > MAX_PREVIEW_URL_LENGTH) return false;
  let url: URL;
  try {
    url = new URL(trimmed);
  } catch {
    return false;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return false;
  if (url.username || url.password) return false;
  return Boolean(url.hostname);
}

const REPO_HOST_LABELS: Readonly<Record<string, string>> = {
  "github.com": "GitHub",
  "gitlab.com": "GitLab",
  "gitee.com": "Gitee",
  "codeberg.org": "Codeberg",
  "huggingface.co": "Hugging Face",
};

export type RepoCoordinates = { host: string; owner: string; repo: string };

/** owner/repo：优先用 Q42 写入的 meta，缺失时从 URL 的前两段路径解析。 */
export function repoCoordinates(site: Pick<CoverSite, "meta" | "originalUrl">): RepoCoordinates | null {
  const meta = libraryRepoMeta(site.meta);
  if (meta) return { host: meta.host.replace(/^www\./u, ""), owner: meta.owner, repo: meta.repo };
  try {
    const url = new URL(site.originalUrl);
    const segments = url.pathname.split("/").filter(Boolean);
    // Hugging Face 的数据集与 Space 带一层类型前缀：/datasets/{owner}/{repo}
    const offset = segments[0] === "datasets" || segments[0] === "spaces" ? 1 : 0;
    const owner = segments[offset];
    const repo = segments[offset + 1]?.replace(/\.git$/u, "");
    if (!owner || !repo) return null;
    return { host: siteDomain(site.originalUrl), owner: decodeURIComponent(owner), repo: decodeURIComponent(repo) };
  } catch {
    return null;
  }
}

/** “GitHub 仓库” / “代码仓库”。 */
export function repoHostLabel(host: string): string | null {
  return REPO_HOST_LABELS[host.toLowerCase().replace(/^www\./u, "")] ?? null;
}

/** 星数格式：999 → “999”，1234 → “1.2k”，34567 → “3.4 万”（截断不进位）。 */
export function formatStars(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "0";
  const whole = Math.floor(value);
  if (whole < 1000) return String(whole);
  if (whole < 10_000) {
    const thousands = Math.floor(whole / 100) / 10;
    return `${Number.isInteger(thousands) ? thousands.toFixed(0) : thousands.toFixed(1)}k`;
  }
  const tenThousands = Math.floor(whole / 1000) / 10;
  return `${Number.isInteger(tenThousands) ? tenThousands.toFixed(0) : tenThousands.toFixed(1)} 万`;
}

function readMetaRecord(meta: LibrarySiteMeta, key: string): Record<string, unknown> | null {
  const value = meta[key];
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** 仓库语言与星数。Q45 写在 meta.github 下；这里两处都认，缺失就不显示。 */
export function repoStats(meta: LibrarySiteMeta): { language: string | null; stars: string | null } {
  const github = readMetaRecord(meta, "github") ?? {};
  const language = [github.language, meta.language].find(
    (value): value is string => typeof value === "string" && value.trim().length > 0,
  );
  const stars = [github.stars, meta.stars].find(
    (value): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0,
  );
  return {
    language: language?.trim() ?? null,
    stars: stars === undefined ? null : formatStars(stars),
  };
}

function summaryText(site: Pick<CoverSite, "summary" | "description">): string | null {
  return site.summary?.trim() || site.description?.trim() || null;
}

function generatedCover(site: CoverSite, size: CoverSize, domain: string, hue: number): GeneratedCover {
  const base = { size, domain, hue };
  if (site.kind === "repo") {
    const coordinates = repoCoordinates(site);
    if (coordinates) {
      return {
        ...base,
        type: "repo",
        ...coordinates,
        ...repoStats(site.meta),
        blurb: summaryText(site),
      };
    }
  }
  if (site.kind === "article") {
    const hook = site.summary?.trim();
    if (hook) return { ...base, type: "article", hook };
  }
  if (site.kind === "video") return { ...base, type: "video" };
  return { ...base, type: "site", name: site.name, faviconUrl: site.faviconUrl };
}

/** 决定一条收藏的封面形态。纯函数；`size` 只影响组件的排版，不改变类型选择。 */
export function coverModel(site: CoverSite, size: CoverSize): CoverModel {
  const domain = siteDomain(site.originalUrl);
  const hue = siteHue(domain);
  const fallback = generatedCover(site, size, domain, hue);
  if (isSafePreviewUrl(site.previewUrl)) {
    return {
      size,
      domain,
      hue,
      type: "image",
      src: site.previewUrl.trim(),
      aspect: site.kind === "video" ? "16 / 9" : "1.91 / 1",
      fallback,
    };
  }
  return fallback;
}

/**
 * 卡片悬停遮罩里的摘要。要点封面已经把简介当作钩子画在封面上，
 * 遮罩再写一遍就是重复：此时改用详细介绍，没有就不写。
 */
export function cardOverlayText(site: CoverSite): string {
  const summary = summaryText(site) ?? "";
  const cover = coverModel(site, "card");
  if (cover.type !== "article") return summary;
  const description = site.description?.trim() ?? "";
  return description && description !== cover.hook ? description : "";
}

/* ── 卡片尺寸估算（瀑布流分列用） ───────────────────────────── */

/** 图片封面的最小高度：保证悬停遮罩放得下摘要与操作行。 */
export const CARD_IMAGE_MIN_HEIGHT = 168;
export const CARD_REPO_HEIGHT = 196;
export const CARD_SITE_HEIGHT = 176;
export const CARD_ARTICLE_MIN_HEIGHT = 200;
/** 标题区：两行标题（14px × 1.5）+ 间距 + 元信息行 + 与封面的间距。 */
export const CARD_CAPTION_HEIGHT = 78;

function weightedLength(text: string): number {
  let length = 0;
  for (const char of text) {
    // 中日韩字符按满宽计，其余（拉丁字母、数字、标点）约半宽
    length += /[⺀-鿿가-힯豈-﫿＀-￯]/u.test(char) ? 1 : 0.55;
  }
  return length;
}

/** 估算一张卡片封面在给定列宽下的高度（px）。 */
export function estimateCoverHeight(model: CoverModel, width: number): number {
  const columnWidth = Number.isFinite(width) && width > 0 ? width : 300;
  if (model.type === "image") {
    const ratio = model.aspect === "16 / 9" ? 16 / 9 : 1.91;
    return Math.max(CARD_IMAGE_MIN_HEIGHT, Math.round(columnWidth / ratio));
  }
  if (model.type === "repo") return CARD_REPO_HEIGHT;
  if (model.type === "site") return CARD_SITE_HEIGHT;
  if (model.type === "video") return Math.max(CARD_IMAGE_MIN_HEIGHT, Math.round((columnWidth * 9) / 16));
  // 要点封面：上下内边距 40 + 引号 22 + 两处间距 36 + 标签 18 + 正文行高 27
  const charsPerLine = Math.max(6, Math.floor((columnWidth - 40) / 18));
  const lines = Math.max(1, Math.ceil(weightedLength(model.hook) / charsPerLine));
  return Math.max(CARD_ARTICLE_MIN_HEIGHT, 116 + lines * 27);
}

/** 整张卡片（封面 + 标题区）的估算高度。 */
export function estimateCardHeight(site: CoverSite, width: number): number {
  return estimateCoverHeight(coverModel(site, "card"), width) + CARD_CAPTION_HEIGHT;
}

/* ── 文案 ─────────────────────────────────────────────────── */

const KIND_LABELS: Readonly<Record<LibrarySiteKind, string>> = {
  repo: "代码仓库",
  article: "帖子与文章",
  video: "视频",
  website: "工具与网站",
};

/** 主卡元信息行里的类型名：GitHub 仓库 / 帖子与文章 / 视频 / 工具与网站。 */
export function resourceKindLabel(site: Pick<CoverSite, "kind" | "meta" | "originalUrl">): string {
  if (site.kind === "repo") {
    const coordinates = repoCoordinates(site);
    const host = coordinates ? repoHostLabel(coordinates.host) : null;
    if (host) return `${host} 仓库`;
  }
  return KIND_LABELS[site.kind];
}

/**
 * 展示用标题：默认是收藏名称；名称只是网址或主机名（快速收藏刚建好、还没整理时）
 * 的仓库退回 owner/repo，其余类型退回“域名 + 路径”。
 * GitHub 网页标题自带的 “GitHub - ” 前缀没有信息量，去掉。
 */
export function resourceTitle(site: Pick<CoverSite, "name" | "originalUrl" | "kind" | "meta">): string {
  const domain = siteDomain(site.originalUrl);
  const name = site.name.trim().replace(/^GitHub\s+-\s+/u, "");
  const bareName = name.toLowerCase().replace(/^www\./u, "").replace(/\/$/u, "");
  const unreadable = !name || /^https?:\/\//iu.test(name) || bareName === domain;
  if (!unreadable) return name;
  if (site.kind === "repo") {
    const coordinates = repoCoordinates(site);
    if (coordinates) return `${coordinates.owner}/${coordinates.repo}`;
  }
  try {
    const url = new URL(site.originalUrl);
    const path = url.pathname === "/" ? "" : url.pathname.replace(/\/$/u, "");
    return `${domain}${path}` || name || site.originalUrl;
  } catch {
    return name || site.originalUrl;
  }
}
