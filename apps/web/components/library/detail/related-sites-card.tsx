"use client";

import { ArrowUpRight, ChevronRight, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { Spinner } from "@/components/react-bits/spinner";
import { SiteFavicon } from "@/components/site-favicon";
import { listLibrarySites } from "@/lib/library-client";
import type { LibrarySite } from "@/lib/library-contract";

import { hostOf } from "./meta";

/* 取舍说明（设计稿 1d「相关网站」）：后端没有「关联网站」算法或字段，
   这里用同分类下最近更新的其他网站近似填充，并在标题旁明确标注「同分类」，
   不谎称这是 Agent 分析出的关联结果。 */

const RELATED_LIMIT = 4;

type RelatedSitesCardProps = {
  siteId: string;
  categoryId: string;
  categoryName: string;
};

type RelatedState =
  | { status: "loading" }
  | { status: "ready"; items: LibrarySite[] }
  | { status: "error"; message: string };

export function RelatedSitesCard({ siteId, categoryId, categoryName }: Readonly<RelatedSitesCardProps>) {
  const [state, setState] = useState<RelatedState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  // 初始态即 loading，重试在点击事件里先置回 loading，效果内不做同步 setState；
  // categoryId 变化时由父组件用 key 重挂载本组件来回到 loading 态
  useEffect(() => {
    const controller = new AbortController();

    // 多取一条：当前站点本身也在同分类里，排除后仍能凑满 4 条
    void listLibrarySites(
      { categoryId, sort: "updated", direction: "desc", limit: RELATED_LIMIT + 1 },
      controller.signal,
    )
      .then((page) => {
        const items = page.items.filter((item) => item.id !== siteId).slice(0, RELATED_LIMIT);
        setState({ status: "ready", items });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "相关网站加载失败，请稍后重试。",
        });
      });

    return () => controller.abort();
  }, [attempt, categoryId, siteId]);

  return (
    <section className="sd-card sd-side-card" aria-labelledby="sd-related-title">
      <header className="sd-side-head">
        <h2 id="sd-related-title" className="sd-side-title">
          相关网站
        </h2>
        <span className="sd-side-badge">同分类</span>
        <span className="sd-side-spacer" aria-hidden="true" />
        <Link className="sd-side-more" href="/library">
          更多
          <ChevronRight size={16} aria-hidden="true" />
        </Link>
      </header>

      {state.status === "loading" && (
        <p className="sd-hint" role="status">
          <Spinner size={16} />
          正在加载同分类网站…
        </p>
      )}

      {state.status === "error" && (
        <div className="sd-inline-error" role="alert">
          <p>{state.message}</p>
          <button
            type="button"
            className="sd-btn sd-btn-secondary sd-btn-small"
            onClick={() => {
              setState({ status: "loading" });
              setAttempt((current) => current + 1);
            }}
          >
            <RefreshCw size={16} aria-hidden="true" />
            重试
          </button>
        </div>
      )}

      {state.status === "ready" &&
        (state.items.length > 0 ? (
          <ul className="sd-related-list">
            {state.items.map((item) => (
              <li key={item.id}>
                <Link className="sd-related-item" href={`/library/${encodeURIComponent(item.id)}`}>
                  <SiteFavicon url={item.faviconUrl} name={item.name} size={22} />
                  <span className="sd-related-body">
                    <span className="sd-related-name">{item.name}</span>
                    <span className="sd-related-desc">
                      {/* 描述可能是空字符串（?? 只兜 null/undefined），这里显式判空后回落到主机名 */}
                      {item.description?.trim()
                        ? item.description
                        : hostOf(item.identityUrl || item.originalUrl)}
                    </span>
                  </span>
                  <ArrowUpRight size={16} className="sd-related-arrow" aria-hidden="true" />
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="sd-hint">「{categoryName}」分类下暂时没有其他网站。</p>
        ))}
    </section>
  );
}
