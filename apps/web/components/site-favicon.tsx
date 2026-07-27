"use client";

import { useState } from "react";

// 共享网站图标：优先展示后端返回的 faviconUrl，绝不走第三方 CDN；
// url 为空或加载失败时，回落到本地生成的字母块（站点名首字符）。
// 字母块的 SVG 是 data: URI，但 <img>/mask 里的 SVG 文档读不到页面 CSS 变量，
// 所以把它当 CSS mask 用：底色/字色仍走 --surface-subtle / --text-muted 令牌，
// 深浅主题切换时颜色能实时正确，不需要写死任何色值。

type SiteFaviconProps = {
  url: string | null;
  name: string;
  size: number;
};

const XML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&apos;",
};

const AVATAR_PALETTES = [
  // Google 蓝
  { bg: "linear-gradient(135deg, #4285F4, #2B66CC)", color: "#FFFFFF" },
  // 翡翠绿
  { bg: "linear-gradient(135deg, #0F9D58, #0B8043)", color: "#FFFFFF" },
  // 珊瑚红
  { bg: "linear-gradient(135deg, #DB4437, #B31412)", color: "#FFFFFF" },
  // 暖阳金
  { bg: "linear-gradient(135deg, #F4B400, #C58F00)", color: "#FFFFFF" },
  // 深度紫
  { bg: "linear-gradient(135deg, #673AB7, #512DA8)", color: "#FFFFFF" },
  // 青瓷绿
  { bg: "linear-gradient(135deg, #00ACC1, #00838F)", color: "#FFFFFF" },
  // 蔷薇粉
  { bg: "linear-gradient(135deg, #E91E63, #C2185B)", color: "#FFFFFF" },
  // 靛青蓝
  { bg: "linear-gradient(135deg, #3F51B5, #303F9F)", color: "#FFFFFF" },
  // 极光青
  { bg: "linear-gradient(135deg, #00897B, #00695C)", color: "#FFFFFF" },
  // 活力橙
  { bg: "linear-gradient(135deg, #FF7043, #E64A19)", color: "#FFFFFF" },
  // 墨镜蓝
  { bg: "linear-gradient(135deg, #5C6BC0, #3949AB)", color: "#FFFFFF" },
  // 亮丽紫
  { bg: "linear-gradient(135deg, #8E24AA, #6A1B9A)", color: "#FFFFFF" },
] as const;

function getAvatarPalette(name: string) {
  let hash = 0;
  const str = name.trim() || "?";
  for (let i = 0; i < str.length; i++) {
    hash = (hash * 31 + str.charCodeAt(i)) | 0;
  }
  const index = Math.abs(hash) % AVATAR_PALETTES.length;
  return AVATAR_PALETTES[index] ?? AVATAR_PALETTES[0];
}

function letterMaskUri(name: string): string {
  // Array.from 按码点切分，中文等多字节字符也能取到完整首字符
  const glyph = (Array.from(name.trim())[0] ?? "?").toLocaleUpperCase();
  const escaped = glyph.replace(/[&<>"']/g, (char) => XML_ESCAPES[char] ?? char);
  // 字号/字重按设计稿定为 47/700（viewBox 100 下约合字块的 47%）
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' +
    '<text x="50" y="50" font-family="system-ui, sans-serif" font-size="47" font-weight="700" ' +
    `text-anchor="middle" dominant-baseline="central">${escaped}</text></svg>`;
  // 中文字符直接放进 data: URI 会产生非法 URL，必须整体编码
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
}

// 小尺寸（≤20，如"最近收录"行）用 4px 圆角，常规尺寸用 5px：
// 圆角随 size 自动选择后，调用方不再需要用外层 overflow 裁切来压圆角
function faviconRadius(size: number): string {
  return size <= 20 ? "var(--radius-xs)" : "var(--radius-sm)";
}

export function SiteFavicon({ url, name, size }: Readonly<SiteFaviconProps>) {
  // 记录加载失败的具体 url：换了新地址会自动重试，而不是永远停在字母块
  const [failedUrl, setFailedUrl] = useState<string | null>(null);

  if (url && url !== failedUrl) {
    return (
      // 后端返回的 favicon 域名不可枚举，无法进 next/image 的静态白名单
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={url}
        alt={`${name} 网站图标`}
        width={size}
        height={size}
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={() => setFailedUrl(url)}
        style={{
          width: size,
          height: size,
          borderRadius: faviconRadius(size),
          objectFit: "contain",
          display: "block",
          flexShrink: 0,
        }}
      />
    );
  }

  const mask = letterMaskUri(name);
  const palette = getAvatarPalette(name);
  return (
    <span
      role="img"
      aria-label={`${name} 网站图标`}
      style={{
        width: size,
        height: size,
        display: "block",
        flexShrink: 0,
        background: palette.bg,
        borderRadius: faviconRadius(size),
        overflow: "hidden",
        boxShadow: "inset 0 0 0 1px rgba(255, 255, 255, 0.15)",
      }}
    >
      <span
        aria-hidden="true"
        style={{
          display: "block",
          width: "100%",
          height: "100%",
          backgroundColor: palette.color,
          WebkitMaskImage: mask,
          maskImage: mask,
          WebkitMaskRepeat: "no-repeat",
          maskRepeat: "no-repeat",
          WebkitMaskSize: "100% 100%",
          maskSize: "100% 100%",
        }}
      />
    </span>
  );
}
