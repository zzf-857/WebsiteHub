"use client";

import { ArrowUpRight, Box } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  ErrorNotice,
  errorText,
  isAbortError,
  SectionHeading,
} from "@/components/home/home-shared";
import { OpenAllDialog } from "@/components/spaces/open-all-dialog";
import { listSpaces } from "@/lib/space-client";
import type { Space } from "@/lib/space-contract";

// 首页「Space 快速入口」分区（设计稿 1a 行 121–136）。
// 「全部打开」是 todolist V0.0.2 的核心诉求：点击后弹确认浮层，
// 让用户选择在新标签页还是当前窗口打开，并提示弹窗拦截风险。

type ListState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; spaces: Space[] };

type ConfirmState = { space: Space };

/* 骨架数与 listSpaces 的 limit 保持一致：数量不齐会在数据到达时撑高容器，产生 CLS */
const SKELETON_COUNT = 8;

export function SpaceShortcuts() {
  const [state, setState] = useState<ListState>({ status: "loading" });
  const [reloadKey, setReloadKey] = useState(0);
  const [confirmState, setConfirmState] = useState<ConfirmState | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      setState({ status: "loading" });
      try {
        const page = await listSpaces({ sort: "updated", limit: 8 }, controller.signal);
        if (controller.signal.aborted) return;
        setState({ status: "ready", spaces: page.items });
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        setState({ status: "error", message: errorText(error, "Space 加载失败") });
      }
    };
    void load();
    return () => controller.abort();
  }, [reloadKey]);

  // 只记住选中的 Space；成员拉取与打开逻辑都在共享弹层里，
  // 首页、/spaces 列表页、详情页三处共用同一份，行为不会各自漂移。
  const openConfirm = useCallback((space: Space) => {
    setConfirmState({ space });
  }, []);

  const closeConfirm = useCallback(() => {
    setConfirmState(null);
  }, []);

  return (
    <section className="home-section" aria-label="Space 快速入口">
      <SectionHeading
        icon={Box}
        title="Space"
        actionHref="/spaces"
        actionLabel="全部 Space →"
      />

      {state.status === "loading" && (
        <div className="home-card-grid" aria-hidden="true">
          {Array.from({ length: SKELETON_COUNT }, (_, index) => (
            <div className="home-space-card home-skeleton-card" key={index}>
              <span className="home-space-copy">
                <span className="home-skeleton-bar" style={{ width: "56%", height: 12 }} />
                <span className="home-skeleton-bar" style={{ width: "38%", height: 10 }} />
              </span>
              <span className="home-skeleton-block" style={{ width: 76, height: 27 }} />
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

      {state.status === "ready" && state.spaces.length === 0 && (
        <p className="home-empty">
          还没有 Space，<Link href="/spaces">去创建一个</Link>，把常用网站装进去。
        </p>
      )}

      {state.status === "ready" && state.spaces.length > 0 && (
        <div className="home-card-grid">
          {state.spaces.map((space) => (
            <div className="home-space-card" key={space.id}>
              <Link
                href={`/spaces/${encodeURIComponent(space.id)}`}
                className="home-space-copy"
                title={space.name}
              >
                <span className="home-space-name">{space.name}</span>
                <span className="home-space-count">{space.memberCount} 个网站</span>
              </Link>
              <button
                type="button"
                className="home-space-open"
                onClick={() => openConfirm(space)}
              >
                全部打开
                <ArrowUpRight aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      )}

      {confirmState && (
        <OpenAllDialog space={confirmState.space} onClose={closeConfirm} />
      )}
    </section>
  );
}
