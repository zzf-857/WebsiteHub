/**
 * 外部图片统一走后端图片代理（Q56）。
 *
 * 网站图标、封面预览图、订阅缩略图都来自第三方网页，浏览器不再直连这些地址：
 * 把绝对 http(s) 地址映射成同源的 `/api/backend/media/proxy?url=…`，由后端带登录态
 * 校验、逐跳 SSRF 复验、魔数校验、大小上限与 SVG 严格策略后从磁盘缓存返回。
 *
 * 订阅缩略图等其他展示外部图片的组件也应调用 `proxiedImageUrl`，不要自己拼地址。
 */

/** next.config.ts 把 /api/backend/* 重写到后端 /api/*。 */
export const MEDIA_PROXY_PATH = "/api/backend/media/proxy";

/** 与后端 `media.proxy.MAX_URL_LENGTH` 一致；更长的地址后端只会 422。 */
export const MAX_PROXIED_IMAGE_URL_LENGTH = 4096;

/**
 * 外部图片地址 → 代理地址；不可代理的值（空、相对地址、data:/javascript: 等非 http(s)、
 * 含账号密码、超长）返回 null，调用方应回落到本地生成的图标或封面。
 * 已经是代理地址的值原样返回，重复调用是安全的。
 */
export function proxiedImageUrl(value: string | null | undefined): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (trimmed.startsWith(`${MEDIA_PROXY_PATH}?`)) return trimmed;
  if (trimmed.length > MAX_PROXIED_IMAGE_URL_LENGTH) return null;

  let url: URL;
  try {
    url = new URL(trimmed);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  if (url.username || url.password || !url.hostname) return null;
  // 片段只在浏览器侧有意义，去掉后同一图片的不同写法共用一份后端缓存
  url.hash = "";
  const target = url.href;
  if (target.length > MAX_PROXIED_IMAGE_URL_LENGTH) return null;
  return `${MEDIA_PROXY_PATH}?url=${encodeURIComponent(target)}`;
}
