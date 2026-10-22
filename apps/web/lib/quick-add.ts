// 顶栏“收藏”快速收藏的纯逻辑：输入校验、默认名称与重复判断（Q44）。

import { MAX_LIBRARY_SITE_NAME_LENGTH } from "./library-contract.ts";

export type QuickAddParse =
  | { ok: true; url: string; name: string }
  | { ok: false; error: string };

const MAX_URL_LENGTH = 2048;
/** 没写协议但看起来像域名（含点、无空白）时自动补 https://，省掉一次手打。 */
const BARE_HOST = /^[^\s/:?#]+\.[^\s/:?#]+(?::\d+)?(?:[/?#]\S*)?$/u;

export function parseQuickAddUrl(input: string): QuickAddParse {
  const trimmed = input.trim();
  if (!trimmed) return { ok: false, error: "先粘贴一个网址" };
  if (trimmed.length > MAX_URL_LENGTH) return { ok: false, error: "网址太长了" };
  const candidate = /^[a-z][a-z\d+.-]*:/iu.test(trimmed) || !BARE_HOST.test(trimmed)
    ? trimmed
    : `https://${trimmed}`;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return { ok: false, error: "这不是有效的网址" };
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    return { ok: false, error: "只支持 http:// 或 https:// 开头的网址" };
  }
  if (!url.hostname || url.username || url.password) {
    return { ok: false, error: "这不是有效的网址" };
  }
  // 名称先用主机名，后台整理完成后会换成网页标题与简介
  const host = url.hostname.toLowerCase().replace(/^www\./u, "");
  return {
    ok: true,
    url: url.toString(),
    name: Array.from(host).slice(0, MAX_LIBRARY_SITE_NAME_LENGTH).join(""),
  };
}

/** 创建接口的 409（`duplicate_url`）表示同一账号已收藏过这个网址。 */
export function isDuplicateSiteError(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  const { status, code } = error as { status?: unknown; code?: unknown };
  return status === 409 && (code === undefined || code === "duplicate_url");
}

/** 重复收藏拿不到现有项 id，链到收藏库并用网址预填搜索。 */
export function duplicateSiteHref(url: string): string {
  return `/library?q=${encodeURIComponent(url)}`;
}

export function siteDetailHref(siteId: string): string {
  return `/library/${encodeURIComponent(siteId)}`;
}
