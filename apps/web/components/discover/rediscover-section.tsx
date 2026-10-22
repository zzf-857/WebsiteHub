"use client";

import { ArrowUpRight, RefreshCw } from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import Link from "next/link";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import {
  REDISCOVER_TITLE_ID,
  RediscoverEyebrow,
  RediscoverNotice,
  RediscoverPager,
  RediscoverSkeleton,
  RediscoverUpNext,
} from "@/components/discover/rediscover-parts";
import { ResourceCover } from "@/components/discover/resource-cover";
import { useSiteActions } from "@/components/discover/use-site-actions";
import { Spinner } from "@/components/react-bits/spinner";
import { useToast } from "@/components/ui/toast";
import {
  heroActive,
  heroDismissInput,
  heroPosition,
  heroReducer,
  heroToastMessage,
  heroUndoInput,
  heroUpNext,
  initialHeroState,
  type HeroDismissal,
  type HeroEntry,
} from "@/lib/discover-hero";
import { fetchRediscover } from "@/lib/library-client";
import { librarySiteCardSummary } from "@/lib/library-contract";
import { subscribeSiteUpdated } from "@/lib/library-events";
import { siteDetailHref } from "@/lib/quick-add";
import { formatSavedDays } from "@/lib/relative-time";
import { coverModel, resourceKindLabel, resourceTitle } from "@/lib/resource-cover";

// 发现页 A 区“今日重温”（方向稿 B 的主视觉）：左侧大封面，右侧理由与操作。
// 主卡队列与撤销是 lib/discover-hero 的纯 reducer；这里负责取数、调接口与动效。

const REDISCOVER_LIMIT = 4;
const MAX_TAGS = 5;

// motion 的 transition 读不到 CSS 变量，这里复写 --ease-standard 的数值
const EASE_STANDARD: [number, number, number, number] = [0.2, 0.8, 0.2, 1];

type LoadState = { status: "loading" } | { status: "error"; message: string } | { status: "ready"; batchSize: number };

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function RediscoverSection() {
  const [shuffle, setShuffle] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);
  const [load, setLoad] = useState<LoadState>({ status: "loading" });
  const [refreshing, setRefreshing] = useState(false);
  const [hero, dispatch] = useReducer(heroReducer, undefined, () => initialHeroState());
  const pendingRef = useRef(new Map<string, Promise<unknown>>());
  const readyRef = useRef(false);
  const reducedMotion = useReducedMotion();
  const toast = useToast();
  const { openSite, changeSiteState } = useSiteActions();

  useEffect(() => {
    const controller = new AbortController();
    const run = async () => {
      try {
        const result = await fetchRediscover({ limit: REDISCOVER_LIMIT, shuffle }, controller.signal);
        if (controller.signal.aborted) return;
        readyRef.current = true;
        dispatch({ type: "load", entries: result.items });
        setLoad({ status: "ready", batchSize: result.items.length });
      } catch (error) {
        if (controller.signal.aborted || isAbort(error)) return;
        const detail = errorMessage(error, "请稍后重试");
        // 已有内容时“换一批”失败只提示，不把整块换成错误面板
        if (readyRef.current) toast.show({ message: `换一批没有成功：${detail}`, tone: "danger" });
        else setLoad({ status: "error", message: `今日重温没有读出来：${detail}` });
      } finally {
        if (!controller.signal.aborted) setRefreshing(false);
      }
    };
    void run();
    return () => controller.abort();
  }, [shuffle, reloadKey, toast]);

  // 画廊里的打开 / 星标 / 已看同步到主卡队列
  useEffect(() => subscribeSiteUpdated((site) => dispatch({ type: "update", site })), []);

  const reshuffle = useCallback(() => {
    setRefreshing(true);
    setShuffle((value) => value + 1);
  }, []);

  const retry = useCallback(() => {
    setLoad({ status: "loading" });
    setReloadKey((value) => value + 1);
  }, []);

  const undo = useCallback(
    async (entry: HeroEntry, dismissal: HeroDismissal) => {
      const input = heroUndoInput(dismissal);
      if (!input) return;
      dispatch({ type: "restore", id: entry.site.id });
      // 等原请求落地再发反向更新，避免两次写入在服务端乱序
      await pendingRef.current.get(entry.site.id)?.catch(() => undefined);
      try {
        await changeSiteState(entry.site, input);
      } catch {
        dispatch({ type: "dismiss", id: entry.site.id, dismissal });
        toast.show({ message: "撤销没有成功，请稍后重试", tone: "danger" });
      }
    },
    [changeSiteState, toast],
  );

  const dismiss = useCallback(
    (entry: HeroEntry, dismissal: HeroDismissal) => {
      dispatch({ type: "dismiss", id: entry.site.id, dismissal });
      if (dismissal === "open") {
        openSite(entry.site);
        return;
      }
      const input = heroDismissInput(dismissal);
      const message = heroToastMessage(dismissal);
      if (!input || !message) return;
      const request = changeSiteState(entry.site, input).catch((error: unknown) => {
        dispatch({ type: "restore", id: entry.site.id });
        toast.show({ message: `没有保存成功，已放回：${errorMessage(error, "请稍后重试")}`, tone: "danger" });
        throw error;
      });
      pendingRef.current.set(entry.site.id, request);
      request.catch(() => undefined);
      toast.show({
        message,
        action: { label: "撤销", onAction: () => void undo(entry, dismissal) },
      });
    },
    [changeSiteState, openSite, toast, undo],
  );

  const select = useCallback((id: string) => dispatch({ type: "select", id }), []);

  if (load.status === "loading") {
    return (
      <section className="discover-section discover-rediscover" aria-labelledby={REDISCOVER_TITLE_ID}>
        <RediscoverSkeleton />
      </section>
    );
  }

  if (load.status === "error") {
    return (
      <section className="discover-section discover-rediscover" aria-labelledby={REDISCOVER_TITLE_ID}>
        <RediscoverEyebrow />
        <RediscoverNotice tone="error" message={load.message} actionLabel="重试" onAction={retry} />
      </section>
    );
  }

  const active = heroActive(hero);
  if (!active) {
    const exhausted = load.batchSize > 0;
    return (
      <section className="discover-section discover-rediscover" aria-labelledby={REDISCOVER_TITLE_ID}>
        <RediscoverEyebrow />
        <RediscoverNotice
          tone={exhausted ? "done" : "empty"}
          message={
            exhausted
              ? "这一批都处理完了。还想再翻翻的话，换一批看看。"
              : "收藏都看过了。新收藏满 3 天后，没打开过的会出现在这里。"
          }
          actionLabel={exhausted ? "换一批" : undefined}
          busy={refreshing}
          onAction={exhausted ? reshuffle : undefined}
        />
      </section>
    );
  }

  const { index, total } = heroPosition(hero);
  const site = active.site;
  const title = resourceTitle(site);
  const summary = librarySiteCardSummary(site);
  // 要点与仓库封面已经把简介画在左侧大封面上，文字列不再重复
  const coverType = coverModel(site, "hero").type;
  const showSummary = summary && coverType !== "article" && coverType !== "repo" && !active.reason.includes(summary);
  const upNext = heroUpNext(hero);
  const swapIn = reducedMotion
    ? { initial: false as const, animate: { opacity: 1, y: 0 }, exit: { opacity: 0, transition: { duration: 0 } }, transition: { duration: 0 } }
    : {
        initial: { opacity: 0, y: 8 },
        animate: { opacity: 1, y: 0, transition: { duration: 0.24, ease: EASE_STANDARD } },
        exit: { opacity: 0, y: 8, transition: { duration: 0.2, ease: EASE_STANDARD } },
      };

  return (
    <section className="discover-section discover-rediscover" aria-labelledby={REDISCOVER_TITLE_ID}>
      <div className="discover-hero" data-refreshing={refreshing || undefined}>
        <div className="discover-hero-cover">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div key={site.id} className="discover-hero-cover-frame" {...swapIn}>
              <Link className="discover-hero-cover-link" href={siteDetailHref(site.id)} aria-label={`查看详情：${title}`}>
                <ResourceCover site={site} size="hero" />
              </Link>
            </motion.div>
          </AnimatePresence>
        </div>

        <div className="discover-hero-body">
          <RediscoverEyebrow>
            <span className="discover-hero-count" aria-label={`第 ${index + 1} 条，共 ${total} 条`}>
              {index + 1} / {total}
            </span>
            <span className="discover-hero-eyebrow-spacer" />
            <RediscoverPager entries={hero.entries} activeId={hero.activeId} onSelect={select} />
          </RediscoverEyebrow>

          <AnimatePresence mode="wait" initial={false}>
            <motion.div key={site.id} className="discover-hero-copy" {...swapIn}>
              <p className="discover-hero-meta">
                <span>{resourceKindLabel(site)}</span>
                <span aria-hidden="true">·</span>
                <span>{formatSavedDays(active.savedDays)}</span>
                <span aria-hidden="true">·</span>
                <span>从未打开</span>
              </p>
              <h3 className="discover-hero-title">{title}</h3>
              <p className="discover-hero-reason">{active.reason}</p>
              {showSummary && <p className="discover-hero-summary">{summary}</p>}
              {site.tags.length > 0 && (
                <p className="discover-hero-tags" aria-label="标签">
                  {site.tags.slice(0, MAX_TAGS).map((tag) => (
                    <span key={tag.id}>#{tag.name}</span>
                  ))}
                </p>
              )}
              <div className="discover-hero-actions">
                <button className="discover-button" data-variant="primary" type="button" onClick={() => dismiss(active, "open")}>
                  打开
                  <ArrowUpRight aria-hidden="true" />
                </button>
                <button className="discover-button" data-variant="secondary" type="button" onClick={() => dismiss(active, "seen")}>
                  已看过
                </button>
                <button className="discover-button" data-variant="text" type="button" onClick={() => dismiss(active, "snooze")}>
                  下周再提醒
                </button>
                <button className="discover-button" data-variant="text" type="button" onClick={() => dismiss(active, "mute")}>
                  不再推荐
                </button>
              </div>
            </motion.div>
          </AnimatePresence>

          <div className="discover-hero-spacer" />

          <div className="discover-hero-next">
            <div className="discover-hero-next-head">
              <span className="discover-hero-next-label">{upNext.length > 0 ? "接下来" : "今天就这一条"}</span>
              <button className="discover-text-button" type="button" onClick={reshuffle} disabled={refreshing}>
                {refreshing ? <Spinner size={14} /> : <RefreshCw aria-hidden="true" />}
                换一批
              </button>
            </div>
            <RediscoverUpNext entries={upNext} onSelect={select} />
          </div>
        </div>
      </div>
    </section>
  );
}
