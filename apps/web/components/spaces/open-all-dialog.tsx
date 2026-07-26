"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { getSpace } from "@/lib/space-client";
import {
  currentWindowNewTabTargets,
  currentWindowTarget,
  mergeAttempt,
  pendingTargets,
  type OpenAllMode,
} from "@/lib/open-all";

/** 成员分页拉取的兜底页数上限，防御异常游标造成的死循环（100 条/页） */
const MAX_MEMBER_PAGES = 5;

export type OpenAllTarget = { id: string; name: string };

/**
 * 在新标签页打开一个地址，返回是否成功（false = 视为被弹窗拦截）。
 *
 * 为什么不能把 "noopener" 放进 window.open 的 features：
 * 依据 HTML 规范（window open steps：features 解析出 noopener 时走
 * "no-opener" 分支，算法直接返回 null），此时 window.open **一律返回 null**，
 * 打开成功与被拦截无法区分——会把每个成功打开的标签都误计为被拦截。
 * 所以这里不传 features，改为拿到窗口引用后手动切断 opener，
 * 既保留 noopener 的安全语义（新页面拿不到本页引用，防钓鱼式回跳），
 * 又能真实检测拦截。请不要改回 features 传 "noopener" 的写法。
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

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/**
 * 「一键全部打开」的共享弹层。
 *
 * 抽出来是因为它此前只存在于首页，且只覆盖最近更新的 8 个 Space——
 * 第 9 个及以后全站没有入口。现在 /spaces 列表页与详情页复用同一份。
 */
export function OpenAllDialog({
  space,
  onClose,
}: Readonly<{ space: OpenAllTarget; onClose: () => void }>) {
  const [urls, setUrls] = useState<string[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // 已成功打开的地址。重试时只针对剩下的——这正是之前会重复开标签的地方。
  const [opened, setOpened] = useState<string[]>([]);
  const [blockedCount, setBlockedCount] = useState<number | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    abortRef.current = controller;
    const load = async () => {
      try {
        const collected: string[] = [];
        let cursor: string | undefined;
        for (let page = 0; page < MAX_MEMBER_PAGES; page += 1) {
          const detail = await getSpace(
            space.id,
            { limit: 100, ...(cursor ? { cursor } : {}) },
            controller.signal,
          );
          for (const member of detail.members) collected.push(member.site.originalUrl);
          if (!detail.nextCursor) break;
          cursor = detail.nextCursor;
        }
        if (controller.signal.aborted) return;
        setUrls(collected);
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        setLoadError(errorText(error, "获取网站列表失败"));
      }
    };
    void load();
    return () => controller.abort();
  }, [space.id]);

  useEffect(() => {
    dialogRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const handleOpenAll = useCallback(
    (mode: OpenAllMode) => {
      if (!urls || urls.length === 0) return;

      if (mode === "tabs") {
        const targets = pendingTargets(urls, opened);
        if (targets.length === 0) {
          onClose();
          return;
        }
        const blocked = targets.filter((url) => !openInNewTab(url));
        const nextOpened = mergeAttempt(opened, targets, blocked);
        setOpened(nextOpened);
        if (blocked.length > 0) {
          setBlockedCount(blocked.length);
          return;
        }
        onClose();
        return;
      }

      // 当前窗口方案：先把其余站点开进新标签，最后才让本页跳走——
      // 若先跳转，本页脚本终止，剩下的站点和重试机会都没了。
      const newTabTargets = currentWindowNewTabTargets(urls, opened);
      const blocked = newTabTargets.filter((url) => !openInNewTab(url));
      const nextOpened = mergeAttempt(opened, newTabTargets, blocked);
      setOpened(nextOpened);
      if (blocked.length > 0) {
        setBlockedCount(blocked.length);
        return;
      }
      const target = currentWindowTarget(urls, nextOpened);
      if (target) window.location.assign(target);
      else onClose();
    },
    [urls, opened, onClose],
  );

  const remaining = urls ? pendingTargets(urls, opened).length : 0;
  const retrying = opened.length > 0;

  return (
    <div className="home-open-overlay" role="presentation" onClick={onClose}>
      <div
        ref={dialogRef}
        className="home-open-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={`打开「${space.name}」的全部网站`}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <p className="home-open-title">全部打开「{space.name}」</p>

        {loadError ? (
          <p className="home-open-error" role="alert">{loadError}</p>
        ) : urls === null ? (
          <p className="home-open-meta">正在获取网站列表…</p>
        ) : urls.length === 0 ? (
          <p className="home-open-meta">这个 Space 还没有网站。</p>
        ) : (
          <>
            <p className="home-open-meta">
              {retrying
                ? `已打开 ${opened.length} / ${urls.length} 个，还剩 ${remaining} 个。`
                : `将打开 ${urls.length} 个网站。`}
            </p>
            {!retrying && urls.length > 12 && (
              <p className="home-open-warning">
                站点较多（{urls.length} 个），请确认后再一次性打开。
              </p>
            )}
            {blockedCount !== null && blockedCount > 0 && (
              <p className="home-open-warning" role="alert">
                有 {blockedCount} 个页面被浏览器拦截。允许本站弹出窗口后再点一次，
                <strong>只会重试没打开的那些</strong>，已打开的不会重复。
              </p>
            )}
            <div className="home-open-actions">
              <button
                type="button"
                className="home-open-primary"
                onClick={() => handleOpenAll("tabs")}
              >
                {retrying ? `重试剩下的 ${remaining} 个` : "在新标签页全部打开"}
              </button>
              {!retrying && (
                <button
                  type="button"
                  className="home-open-secondary"
                  onClick={() => handleOpenAll("current")}
                  // 文案与行为对齐：这个模式不是在同一个标签里逐个走完。
                  title="第一个网站占用当前标签页，其余在新标签页打开"
                >
                  当前标签开第一个，其余开新标签
                </button>
              )}
            </div>
          </>
        )}
        <button type="button" className="home-open-close" onClick={onClose}>
          取消
        </button>
      </div>
    </div>
  );
}
