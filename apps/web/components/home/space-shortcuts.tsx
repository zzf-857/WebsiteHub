"use client";

import { ArrowUpRight, Box } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  ErrorNotice,
  errorText,
  isAbortError,
  SectionHeading,
} from "@/components/home/home-shared";
import { getSpace, listSpaces } from "@/lib/space-client";
import type { Space } from "@/lib/space-contract";

// 首页「Space 快速入口」分区（设计稿 1a 行 121–136）。
// 「全部打开」是 todolist V0.0.2 的核心诉求：点击后弹确认浮层，
// 让用户选择在新标签页还是当前窗口打开，并提示弹窗拦截风险。

type ListState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; spaces: Space[] };

type ConfirmState = {
  space: Space;
  /** null 表示成员列表仍在加载 */
  urls: string[] | null;
  loadError: string | null;
  /** 打开后被浏览器拦截的数量；null 表示还没执行过打开 */
  blockedCount: number | null;
};

/* 骨架数与 listSpaces 的 limit 保持一致：数量不齐会在数据到达时撑高容器，产生 CLS */
const SKELETON_COUNT = 8;
/** 成员分页拉取的兜底页数上限，防御异常游标造成的死循环（100 条/页） */
const MAX_MEMBER_PAGES = 5;

/**
 * 在新标签页打开一个地址，返回是否成功（false = 视为被弹窗拦截）。
 *
 * 为什么不能把 "noopener" 放进 window.open 的 features：
 * 依据 HTML 规范（window open steps：features 解析出 noopener 时走
 * "no-opener" 分支，算法直接返回 null），此时 window.open **一律返回 null**，
 * 打开成功与被拦截无法区分——之前的写法会把每个成功打开的标签都误计为被拦截。
 * 所以这里不传 features，改为拿到窗口引用后手动切断 opener
 * （opened.opener = null），既保留 noopener 的安全语义（新页面拿不到本页引用，
 * 防钓鱼式回跳），又能真实检测拦截。请不要改回 features 传 "noopener" 的写法。
 *
 * 判定逻辑刻意写成保守形式：我们已确认没传 noopener，此前提下
 * window.open 返回 null 或抛异常才计为拦截——即便对规范的理解有出入，
 * 也只可能漏报、不会把成功打开误报成被拦截。
 * 注：此行为属浏览器运行时语义，tsc / lib.dom.d.ts 与 Node 脚本都无法验证，
 * 上述结论以 HTML 规范为准。
 */
function openInNewTab(url: string): boolean {
  try {
    const opened = window.open(url, "_blank");
    if (opened) {
      opened.opener = null;
      return true;
    }
    return false;
  } catch {
    return false;
  }
}

export function SpaceShortcuts() {
  const [state, setState] = useState<ListState>({ status: "loading" });
  const [reloadKey, setReloadKey] = useState(0);
  const [confirmState, setConfirmState] = useState<ConfirmState | null>(null);
  const confirmAbortRef = useRef<AbortController | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);

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

  // 组件卸载时中止仍在进行的成员预取，避免请求泄漏与卸载后 setState
  useEffect(() => () => {
    confirmAbortRef.current?.abort();
  }, []);

  // 打开浮层时立刻预取成员地址：等用户点「全部打开」时地址已就绪，
  // window.open 能贴着点击手势执行，尽量少触发浏览器的弹窗拦截。
  const openConfirm = useCallback((space: Space) => {
    confirmAbortRef.current?.abort();
    const controller = new AbortController();
    confirmAbortRef.current = controller;
    setConfirmState({ space, urls: null, loadError: null, blockedCount: null });

    const load = async () => {
      try {
        const urls: string[] = [];
        let cursor: string | undefined;
        for (let page = 0; page < MAX_MEMBER_PAGES; page += 1) {
          const detail = await getSpace(
            space.id,
            { limit: 100, ...(cursor ? { cursor } : {}) },
            controller.signal,
          );
          for (const member of detail.members) urls.push(member.site.originalUrl);
          if (!detail.nextCursor) break;
          cursor = detail.nextCursor;
        }
        if (controller.signal.aborted) return;
        setConfirmState((prev) =>
          prev && prev.space.id === space.id ? { ...prev, urls } : prev,
        );
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        setConfirmState((prev) =>
          prev && prev.space.id === space.id
            ? { ...prev, loadError: errorText(error, "获取网站列表失败") }
            : prev,
        );
      }
    };
    void load();
  }, []);

  const closeConfirm = useCallback(() => {
    confirmAbortRef.current?.abort();
    setConfirmState(null);
  }, []);

  // 浮层打开时把焦点收进来，并支持 Escape 关闭
  const confirmSpaceId = confirmState?.space.id ?? null;
  useEffect(() => {
    if (!confirmSpaceId) return;
    dialogRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeConfirm();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [confirmSpaceId, closeConfirm]);

  const handleOpenAll = useCallback(
    (mode: "tabs" | "current") => {
      const urls = confirmState?.urls;
      if (!urls || urls.length === 0) return;

      let blocked = 0;
      if (mode === "tabs") {
        for (const url of urls) {
          // 逐个统计被拦截数量，以便给出准确提示（判定细节见 openInNewTab 注释）
          if (!openInNewTab(url)) blocked += 1;
        }
        if (blocked > 0) {
          setConfirmState((prev) => (prev ? { ...prev, blockedCount: blocked } : prev));
          return;
        }
        closeConfirm();
        return;
      }

      // 当前窗口方案：先把其余站点开进新标签，最后才让本页跳走——
      // 若先跳转，本页脚本终止，剩下的站点就永远打不开了。
      for (const url of urls.slice(1)) {
        if (!openInNewTab(url)) blocked += 1;
      }
      if (blocked > 0) {
        setConfirmState((prev) => (prev ? { ...prev, blockedCount: blocked } : prev));
        return;
      }
      window.location.assign(urls[0]);
    },
    [confirmState, closeConfirm],
  );

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
        <div className="home-open-overlay" role="presentation" onClick={closeConfirm}>
          <div
            ref={dialogRef}
            className="home-open-dialog"
            role="dialog"
            aria-modal="true"
            aria-label={`打开「${confirmState.space.name}」的全部网站`}
            tabIndex={-1}
            onClick={(event) => event.stopPropagation()}
          >
            <p className="home-open-title">全部打开「{confirmState.space.name}」</p>

            {confirmState.loadError ? (
              <p className="home-open-error" role="alert">
                {confirmState.loadError}
              </p>
            ) : confirmState.urls === null ? (
              <p className="home-open-meta">正在获取网站列表…</p>
            ) : confirmState.urls.length === 0 ? (
              <p className="home-open-meta">这个 Space 还没有网站。</p>
            ) : (
              <>
                <p className="home-open-meta">将打开 {confirmState.urls.length} 个网站。</p>
                {confirmState.urls.length > 12 && (
                  <p className="home-open-warning">
                    站点较多（{confirmState.urls.length} 个），请确认后再一次性打开。
                  </p>
                )}
                <p className="home-open-hint">若被浏览器拦截，请允许本站弹出窗口后重试。</p>
                {confirmState.blockedCount !== null && confirmState.blockedCount > 0 && (
                  <p className="home-open-warning" role="alert">
                    有 {confirmState.blockedCount} 个页面被浏览器拦截，请允许弹出窗口后重试。
                  </p>
                )}
                <div className="home-open-actions">
                  <button
                    type="button"
                    className="home-open-primary"
                    onClick={() => handleOpenAll("tabs")}
                  >
                    在新标签页全部打开
                  </button>
                  <button
                    type="button"
                    className="home-open-secondary"
                    onClick={() => handleOpenAll("current")}
                  >
                    在当前窗口依次打开
                  </button>
                </div>
              </>
            )}

            <button type="button" className="home-open-cancel" onClick={closeConfirm}>
              取消
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
