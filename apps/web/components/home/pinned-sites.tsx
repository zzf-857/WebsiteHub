"use client";

import { Pin } from "lucide-react";
import { useEffect, useState } from "react";

import {
  ErrorNotice,
  errorText,
  isAbortError,
  SectionHeading,
  siteHostname,
} from "@/components/home/home-shared";
import { SiteFavicon } from "@/components/site-favicon";
import { listLibrarySites } from "@/lib/library-client";
import type { LibrarySite } from "@/lib/library-contract";

// 首页「置顶网站」分区（设计稿 1a 行 106–120）：
// 4 列小卡，整卡可点、新标签打开，hover 只动边框和阴影。

type FetchState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; sites: LibrarySite[] };

const SKELETON_COUNT = 8;

export function PinnedSites() {
  const [state, setState] = useState<FetchState>({ status: "loading" });
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      setState({ status: "loading" });
      try {
        const page = await listLibrarySites(
          { pinned: true, sort: "updated", limit: 8 },
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setState({ status: "ready", sites: page.items });
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        setState({ status: "error", message: errorText(error, "置顶网站加载失败") });
      }
    };
    void load();
    return () => controller.abort();
  }, [reloadKey]);

  return (
    <section className="home-section" aria-label="置顶网站">
      <SectionHeading
        icon={Pin}
        tone="accent"
        title="置顶网站"
        actionHref="/library?pinned=1"
        actionLabel="管理置顶"
      />

      {state.status === "loading" && (
        <div className="home-card-grid" aria-hidden="true">
          {Array.from({ length: SKELETON_COUNT }, (_, index) => (
            <div className="home-pinned-card home-skeleton-card" key={index}>
              <span className="home-skeleton-block" style={{ width: 24, height: 24 }} />
              <span className="home-pinned-copy">
                <span className="home-skeleton-bar" style={{ width: "68%", height: 12 }} />
                <span className="home-skeleton-bar" style={{ width: "46%", height: 10 }} />
              </span>
            </div>
          ))}
        </div>
      )}

      {state.status === "error" && (
        <ErrorNotice
          message={state.message}
          onRetry={() => setReloadKey((key) => key + 1)}
        />
      )}

      {state.status === "ready" && state.sites.length === 0 && (
        <p className="home-empty">还没有置顶网站，在网址库里点星标即可置顶。</p>
      )}

      {state.status === "ready" && state.sites.length > 0 && (
        <div className="home-card-grid">
          {state.sites.map((site) => (
            <a
              key={site.id}
              className="home-pinned-card"
              href={site.originalUrl}
              target="_blank"
              rel="noreferrer noopener"
              title={site.name}
            >
              <SiteFavicon url={site.faviconUrl} name={site.name} size={24} />
              <span className="home-pinned-copy">
                <span className="home-pinned-name">{site.name}</span>
                <span className="home-pinned-host">{siteHostname(site.originalUrl)}</span>
              </span>
            </a>
          ))}
        </div>
      )}
    </section>
  );
}
