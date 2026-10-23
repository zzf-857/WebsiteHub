"use client";

import { ArrowUpRight, BookmarkCheck, BookmarkPlus, Headphones, X } from "lucide-react";
import Link from "next/link";
import { useState, type CSSProperties } from "react";

import { Spinner } from "@/components/react-bits/spinner";
import { formatDuration, itemMoment, type Feed, type FeedItem } from "@/lib/feeds-contract";
import { feedMark, feedMarkSource } from "@/lib/feeds-mark";
import { proxiedImageUrl } from "@/lib/media-proxy";
import { siteDetailHref } from "@/lib/quick-add";
import { formatRelativeTime } from "@/lib/relative-time";

export type FeedItemActions = {
  open: (item: FeedItem) => void;
  save: (item: FeedItem) => void;
  dismiss: (item: FeedItem) => void;
  saving: ReadonlySet<string>;
};

function FeedSourceMark({ feed, title, podcast }: { feed?: Feed; title: string; podcast: boolean }) {
  const mark = feedMark(feed, title);
  const [failedSources, setFailedSources] = useState<ReadonlySet<string>>(new Set());
  const source = feedMarkSource(mark, podcast, failedSources);
  return (
    <span
      className="feed-mark"
      data-kind={podcast ? "podcast" : "rss"}
      style={{ "--site-hue": mark.hue } as CSSProperties}
      aria-hidden="true"
    >
      {source.kind === "image" || source.kind === "favicon" ? (
        // 封面与站点自身 favicon 都经同源代理；失败后继续尝试下一标识。
        // eslint-disable-next-line @next/next/no-img-element
        <img
          key={source.url}
          className={source.kind === "image" ? "feeds-source-cover" : undefined}
          src={source.url}
          alt=""
          width={source.kind === "image" ? 36 : 20}
          height={source.kind === "image" ? 36 : 20}
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setFailedSources((current) => new Set(current).add(source.url))}
        />
      ) : source.kind === "podcast" ? (
        <Headphones size={18} />
      ) : (
        mark.letter
      )}
    </span>
  );
}

export function FeedItemRow({
  item,
  feed,
  source,
  reason,
  now,
  actions,
}: {
  item: FeedItem;
  feed?: Feed;
  source?: string;
  reason?: string;
  now: number;
  actions: FeedItemActions;
}) {
  const [failedThumbnail, setFailedThumbnail] = useState<string | null>(null);
  const imageSrc = proxiedImageUrl(item.imageUrl);
  const thumbnail = imageSrc && imageSrc !== failedThumbnail ? imageSrc : null;
  const duration = formatDuration(item.durationSeconds);
  const moment = formatRelativeTime(itemMoment(item), now);
  const saving = actions.saving.has(item.id);
  const podcast = feed ? feed.kind === "podcast" : Boolean(item.audioUrl);
  return (
    <li
      className="feed-card"
      data-read={item.read ? "true" : undefined}
      data-has-image={thumbnail ? "true" : undefined}
    >
      <FeedSourceMark feed={feed} title={source ?? feed?.title ?? item.title} podcast={podcast} />
      <div className="feed-card-main">
        {item.url ? (
          <a
            className="feed-card-title"
            href={item.url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={() => actions.open(item)}
          >
            <span>{item.title}</span>
            <ArrowUpRight className="feed-card-title-arrow" aria-hidden="true" size={14} />
            <span className="sr-only">（在新标签页打开）</span>
          </a>
        ) : (
          <span className="feed-card-title">{item.title}</span>
        )}
        <p className="feed-card-meta">
          {source && <span className="feed-card-source">{source}</span>}
          {moment && <span>{moment}</span>}
          {item.author && <span>{item.author}</span>}
          {duration && (
            <span className="feed-card-duration">
              <Headphones aria-hidden="true" size={12} />
              {duration}
            </span>
          )}
          {item.audioUrl && (
            <a href={item.audioUrl} target="_blank" rel="noopener noreferrer">
              音频
            </a>
          )}
        </p>
        {item.summary && <p className="feed-card-summary">{item.summary}</p>}
        {reason && <p className="feed-card-reason">{reason}</p>}
      </div>
      {thumbnail && (
        // 图片地址由订阅源提供，统一交给同源代理；失败后移除整个缩略图位。
        // eslint-disable-next-line @next/next/no-img-element
        <img
          className="feeds-item-thumbnail"
          src={thumbnail}
          alt=""
          width={96}
          height={64}
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setFailedThumbnail(thumbnail)}
        />
      )}
      <div className="feed-card-actions">
        {item.savedSiteId ? (
          <Link
            className="feed-action feed-action-icon"
            data-tone="saved"
            data-tooltip="已收藏，查看详情"
            aria-label={`已收藏「${item.title}」，查看详情`}
            href={siteDetailHref(item.savedSiteId)}
          >
            <BookmarkCheck aria-hidden="true" size={16} />
          </Link>
        ) : (
          <button
            className="feed-action feed-action-icon"
            type="button"
            disabled={saving || !item.url}
            onClick={() => actions.save(item)}
            data-tooltip={item.url ? "收进收藏库" : "这个条目没有链接"}
            aria-label={item.url ? `收进收藏库：${item.title}` : "这个条目没有链接，无法收藏"}
          >
            {saving ? <Spinner size={14} /> : <BookmarkPlus aria-hidden="true" size={16} />}
          </button>
        )}
        <button
          className="feed-action feed-action-icon"
          type="button"
          aria-label={`忽略「${item.title}」`}
          data-tooltip="忽略"
          onClick={() => actions.dismiss(item)}
        >
          <X aria-hidden="true" size={16} />
        </button>
      </div>
    </li>
  );
}
