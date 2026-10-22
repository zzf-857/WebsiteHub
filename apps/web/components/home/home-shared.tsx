"use client";

import type { LucideIcon } from "lucide-react";
import { CircleAlert } from "lucide-react";
import Link from "next/link";

// 置顶区与补全工具条共用的小件：分区标题行 / 错误提示 / 若干纯函数。
// Q44 后它们挪到了收藏库顶部，旧首页的其余分区已删除。

type SectionHeadingProps = {
  icon: LucideIcon;
  title: string;
  /** 图标着色：置顶分区用青柠强调色，其余保持中性 */
  tone?: "accent" | "neutral";
  actionHref?: string;
  actionLabel?: string;
};

export function SectionHeading({
  icon: Icon,
  title,
  tone = "neutral",
  actionHref,
  actionLabel,
}: Readonly<SectionHeadingProps>) {
  return (
    <div className="home-section-heading">
      <Icon className="home-section-icon" data-tone={tone} aria-hidden="true" />
      <h2 className="home-section-title">{title}</h2>
      <span className="home-section-spacer" />
      {actionHref && actionLabel ? (
        <Link href={actionHref} className="home-section-action">
          {actionLabel}
        </Link>
      ) : null}
    </div>
  );
}

type ErrorNoticeProps = {
  message: string;
  onRetry: () => void;
};

export function ErrorNotice({ message, onRetry }: Readonly<ErrorNoticeProps>) {
  return (
    <div className="home-error" role="alert">
      <CircleAlert className="home-error-icon" aria-hidden="true" />
      <span className="home-error-text">{message}</span>
      <button type="button" className="home-error-retry" onClick={onRetry}>
        重试
      </button>
    </div>
  );
}

/** 从收录地址提取主机名用于展示；解析失败时回退原始字符串，避免整卡渲染崩掉 */
export function siteHostname(rawUrl: string): string {
  try {
    return new URL(rawUrl).hostname.replace(/^www\./, "");
  } catch {
    return rawUrl;
  }
}

export function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
