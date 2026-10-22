"use client";

import { CodeXml, Play, Star } from "lucide-react";
import { useState, type CSSProperties } from "react";

import { SiteFavicon } from "@/components/site-favicon";
import type { LibrarySite } from "@/lib/library-contract";
import {
  coverModel,
  siteHue,
  type CoverSize,
  type GeneratedCover,
  type RepoCover,
} from "@/lib/resource-cover";

// 资源封面（2026-10 D4）：网页预览图优先，否则按类型生成。
// 颜色全部来自 globals.css 的 --cover-* 令牌；站点色相只作为 --site-hue 变量传入。

type ResourceCoverProps = {
  site: Pick<
    LibrarySite,
    "name" | "originalUrl" | "summary" | "description" | "faviconUrl" | "previewUrl" | "kind" | "meta"
  >;
  size: CoverSize;
};

type CoverStyle = CSSProperties & Record<`--${string}`, string | number>;

function hueStyle(hue: number, extra: CSSProperties = {}): CoverStyle {
  return { ...extra, "--site-hue": hue };
}

/** 引号图形（方向稿 B 的要点封面），用内联 SVG 才能跟随 --cover-quote 着色。 */
function QuoteMark() {
  return (
    <svg className="resource-cover-quote" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M4 11h5v8H3v-6a8 8 0 0 1 4-7l1 2a5 5 0 0 0-3 3zm11 0h5v8h-6v-6a8 8 0 0 1 4-7l1 2a5 5 0 0 0-3 3z" />
    </svg>
  );
}

function RepoStats({ model }: Readonly<{ model: RepoCover }>) {
  // 顶行已经写了托管站点 / owner，没有 Q45 统计数据时底行整行不画，不再重复域名
  if (!model.language && !model.stars) return null;
  return (
    <span className="resource-cover-repo-stats">
      {model.language && (
        <span className="resource-cover-repo-language">
          <span
            className="resource-cover-repo-dot"
            style={hueStyle(siteHue(model.language))}
            aria-hidden="true"
          />
          {model.language}
        </span>
      )}
      {model.stars && (
        <span className="resource-cover-repo-stars" aria-label={`${model.stars} 星`}>
          <Star aria-hidden="true" fill="currentColor" />
          {model.stars}
        </span>
      )}
    </span>
  );
}

function GeneratedCoverView({ model }: Readonly<{ model: GeneratedCover }>) {
  const { size } = model;

  if (model.type === "repo") {
    if (size === "thumb") {
      return (
        <span className="resource-cover" data-type="repo" data-size={size}>
          <span className="resource-cover-repo-name">{model.repo}</span>
        </span>
      );
    }
    return (
      <span className="resource-cover" data-type="repo" data-size={size}>
        <span className="resource-cover-repo-owner">
          {size === "hero" ? (
            <>
              <CodeXml aria-hidden="true" />
              {model.host} / {model.owner}
            </>
          ) : (
            `${model.owner} /`
          )}
        </span>
        <span className="resource-cover-repo-main">
          <span className="resource-cover-repo-name">{model.repo}</span>
          {size === "hero" && model.blurb && (
            <span className="resource-cover-repo-blurb">{model.blurb}</span>
          )}
        </span>
        <RepoStats model={model} />
      </span>
    );
  }

  if (model.type === "article") {
    return (
      <span className="resource-cover" data-type="article" data-size={size}>
        {size !== "thumb" && <QuoteMark />}
        <span className="resource-cover-hook">{model.hook}</span>
        {size !== "thumb" && <span className="resource-cover-label">AI 提炼的要点</span>}
      </span>
    );
  }

  if (model.type === "video") {
    return (
      <span className="resource-cover" data-type="video" data-size={size} style={hueStyle(model.hue)}>
        <span className="resource-cover-play" aria-hidden="true">
          <Play fill="currentColor" />
        </span>
        {size !== "thumb" && <span className="resource-cover-domain">{model.domain}</span>}
      </span>
    );
  }

  const faviconSize = size === "hero" ? 52 : size === "thumb" ? 20 : 32;
  return (
    <span className="resource-cover" data-type="site" data-size={size} style={hueStyle(model.hue)}>
      <span className="resource-cover-tile">
        <SiteFavicon url={model.faviconUrl} name={model.name} size={faviconSize} />
      </span>
      {size !== "thumb" && <span className="resource-cover-domain">{model.domain}</span>}
    </span>
  );
}

export function ResourceCover({ site, size }: Readonly<ResourceCoverProps>) {
  const model = coverModel(site, size);
  // 记录失败的具体地址：预览图换了新地址时自动重试，而不是永远停在生成封面
  const [failedSrc, setFailedSrc] = useState<string | null>(null);

  if (model.type === "image" && model.src !== failedSrc) {
    return (
      <span
        className="resource-cover"
        data-type="image"
        data-size={size}
        style={hueStyle(model.hue, size === "card" ? { aspectRatio: model.aspect } : {})}
      >
        {/* 预览图来自第三方网页，域名不可枚举，无法进 next/image 的静态白名单 */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={model.src}
          alt=""
          // 主卡在首屏，立即加载；卡片与缩略图交给浏览器按需加载
          loading={size === "hero" ? "eager" : "lazy"}
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailedSrc(model.src)}
        />
      </span>
    );
  }

  return <GeneratedCoverView model={model.type === "image" ? model.fallback : model} />;
}
