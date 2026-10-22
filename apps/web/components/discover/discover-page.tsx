"use client";

import { Bot, FileUp } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { GallerySection } from "@/components/discover/gallery-section";
import { QuickAddForm } from "@/components/discover/quick-add";
import { RediscoverSection } from "@/components/discover/rediscover-section";
import { fetchLibraryOverview } from "@/lib/library-client";
import type { LibraryOverview } from "@/lib/library-contract";
import { subscribeLibraryChanged } from "@/lib/library-events";

// 发现页（新首页，Q44）：A 区“今日重温”主视觉 + B 区“全部收藏”封面瀑布流。
// 概览计数在这里取一次：类型标签的计数与“整库为空”的首次使用面板都靠它。
// SiteHeader / AuthGate 由 (workspace)/layout.tsx 提供。

type OverviewState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; overview: LibraryOverview };

const FIRST_RUN_TITLE_ID = "discover-first-run-title";

function FirstRunPanel() {
  return (
    <section className="discover-first-run" aria-labelledby={FIRST_RUN_TITLE_ID}>
      <h2 className="discover-first-run-title" id={FIRST_RUN_TITLE_ID}>
        把第一个收藏放进来
      </h2>
      <p className="discover-first-run-text">
        粘贴一个链接，WebHub 会自动整理类型、标签和简介；收进来却没打开过的，几天后会回到这里提醒你。
      </p>
      <QuickAddForm variant="panel" />
      <div className="discover-first-run-links">
        <Link className="discover-first-run-link" href="/settings/import">
          <FileUp aria-hidden="true" />
          导入浏览器书签
        </Link>
        <Link className="discover-first-run-link" href="/settings/agents">
          <Bot aria-hidden="true" />
          接入你的 Agent
        </Link>
      </div>
    </section>
  );
}

export function DiscoverPage() {
  const [overview, setOverview] = useState<OverviewState>({ status: "loading" });
  const [revision, setRevision] = useState(0);

  useEffect(() => subscribeLibraryChanged(() => setRevision((value) => value + 1)), []);

  useEffect(() => {
    const controller = new AbortController();
    const run = async () => {
      try {
        const result = await fetchLibraryOverview(controller.signal);
        if (!controller.signal.aborted) setOverview({ status: "ready", overview: result });
      } catch {
        // 概览只提供计数：失败时画廊照常工作，只是不显示数字
        if (!controller.signal.aborted) setOverview({ status: "error" });
      }
    };
    void run();
    return () => controller.abort();
  }, [revision]);

  const ready = overview.status === "ready" ? overview.overview : null;
  const firstRun = ready?.total === 0;

  return (
    <main className="discover-page">
      <h1 className="sr-only">发现</h1>
      {firstRun ? (
        <FirstRunPanel />
      ) : (
        <>
          <RediscoverSection />
          <GallerySection overview={ready} revision={revision} />
        </>
      )}
    </main>
  );
}
