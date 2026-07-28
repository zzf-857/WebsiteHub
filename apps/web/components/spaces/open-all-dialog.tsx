"use client";

import { FolderOpen, LoaderCircle } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  BrowserSpaceGroupError,
  clearBrowserSpaceGroupOperation,
  hasUnresolvedBrowserSpaceGroupOperation,
  normalizeBrowserSpaceGroupUrls,
  openSpaceInBrowserGroup,
  probeBrowserSpaceGroups,
  reserveBrowserSpaceGroupOperation,
  type BrowserSpaceGroupOperationReservation,
} from "@/lib/browser-space-groups";
import { getSpace } from "@/lib/space-client";
import {
  currentWindowNewTabTargets,
  currentWindowTarget,
  mergeAttempt,
  pendingTargets,
  type OpenAllMode,
} from "@/lib/open-all";

const MAX_TABS_PER_OPEN = 100;

export type OpenAllTarget = { id: string; name: string; memberCount: number };

type BrowserBridgeState =
  | { status: "checking" }
  | { status: "available"; maxTabs: number }
  | { status: "unavailable" };

type GroupRecoveryState = "checking" | "clear" | "uncertain";

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

function extensionConfirmedRollback(error: unknown, recovery: boolean): boolean {
  if (!(error instanceof BrowserSpaceGroupError)) return false;
  if (error.code === "IDEMPOTENCY_CONFLICT") return true;
  if (recovery && (
    error.code.startsWith("INVALID_") || [
      "TOO_MANY_URLS",
      "EXTENSION_BUSY",
      "STORAGE_FAILED",
    ].includes(error.code)
  )) {
    return false;
  }
  return error.code.startsWith("INVALID_") || [
    "TOO_MANY_URLS",
    "EXTENSION_BUSY",
    "IDEMPOTENCY_CONFLICT",
    "TAB_CREATE_FAILED",
    "TAB_NAVIGATE_FAILED",
    "TAB_GROUP_FAILED",
    "TAB_GROUP_UPDATE_FAILED",
    "TAB_ACTIVATE_FAILED",
    "STORAGE_FAILED",
  ].includes(error.code);
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
  const [bridge, setBridge] = useState<BrowserBridgeState>({ status: "checking" });
  const [groupBusy, setGroupBusy] = useState(false);
  const [groupError, setGroupError] = useState<string | null>(null);
  const [groupRecovery, setGroupRecovery] = useState<GroupRecoveryState>("checking");
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const groupOperationRef = useRef<BrowserSpaceGroupOperationReservation | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    abortRef.current = controller;
    const load = async () => {
      if (space.memberCount > MAX_TABS_PER_OPEN) {
        setLoadError(
          `这个 Space 有 ${space.memberCount} 个网站。单次最多打开 ${MAX_TABS_PER_OPEN} 个，避免浏览器失去响应。`,
        );
        return;
      }
      try {
        const collected: string[] = [];
        const detail = await getSpace(
          space.id,
          { limit: MAX_TABS_PER_OPEN },
          controller.signal,
        );
        for (const member of detail.members) collected.push(member.site.originalUrl);
        if (detail.nextCursor || detail.memberCount > MAX_TABS_PER_OPEN) {
          throw new Error(
            `这个 Space 超过单次 ${MAX_TABS_PER_OPEN} 个标签的安全上限，请拆分后再打开。`,
          );
        }
        if (controller.signal.aborted) return;
        setUrls(normalizeBrowserSpaceGroupUrls(collected));
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        setLoadError(errorText(error, "获取网站列表失败"));
      }
    };
    void load();
    return () => controller.abort();
  }, [space.id, space.memberCount]);

  useEffect(() => {
    let active = true;
    setBridge({ status: "checking" });
    void probeBrowserSpaceGroups().then((result) => {
      if (!active) return;
      setBridge(result
        ? { status: "available", maxTabs: result.maxTabs }
        : { status: "unavailable" });
    });
    return () => {
      active = false;
    };
  }, [space.id]);

  useEffect(() => {
    if (!urls || urls.length === 0) return;
    let active = true;
    setGroupRecovery("checking");
    void hasUnresolvedBrowserSpaceGroupOperation(space.id).then((unresolved) => {
      if (active) setGroupRecovery(unresolved ? "uncertain" : "clear");
    }).catch(() => {
      if (active) setGroupRecovery("uncertain");
    });
    return () => {
      active = false;
    };
  }, [space.id, space.name, urls]);

  useEffect(() => {
    dialogRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !groupBusy) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [groupBusy, onClose]);

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

  const handleBrowserGroup = useCallback(async () => {
    if (!urls || bridge.status !== "available" || groupBusy) return;
    const targets = normalizeBrowserSpaceGroupUrls(pendingTargets(urls, opened));
    if (targets.length === 0) {
      onClose();
      return;
    }
    if (targets.length > Math.min(MAX_TABS_PER_OPEN, bridge.maxTabs)) {
      setGroupError(`浏览器助手单次最多处理 ${bridge.maxTabs} 个标签。`);
      return;
    }

    setGroupBusy(true);
    setGroupError(null);
    let operationId: string | null = null;
    let operation: BrowserSpaceGroupOperationReservation | null = null;
    let requestStarted = false;
    try {
      operation = groupOperationRef.current ?? await reserveBrowserSpaceGroupOperation({
        spaceId: space.id,
        spaceName: space.name,
        urls: targets,
      });
      groupOperationRef.current = operation;
      operationId = operation.operationId;
      if (operation.input.urls.length > Math.min(MAX_TABS_PER_OPEN, bridge.maxTabs)) {
        throw new BrowserSpaceGroupError(
          "TOO_MANY_URLS",
          `浏览器助手单次最多处理 ${bridge.maxTabs} 个标签。`,
        );
      }
      requestStarted = true;
      setGroupRecovery("uncertain");
      const result = await openSpaceInBrowserGroup({
        operationId,
        operationStartedAt: operation.operationStartedAt,
        recovery: operation.recovery,
        ...operation.input,
      });
      if (result.openedCount !== operation.input.urls.length) {
        throw new Error("浏览器返回的分组数量与本次任务不一致。请重试。");
      }
      await clearBrowserSpaceGroupOperation(space.id, operationId);
      groupOperationRef.current = null;
      setGroupRecovery("clear");
      onClose();
    } catch (error) {
      let uncertain = requestStarted
        ? !extensionConfirmedRollback(error, operation?.recovery ?? false)
        : groupRecovery === "uncertain";
      let displayedError = error;
      if (!uncertain && operationId) {
        try {
          await clearBrowserSpaceGroupOperation(space.id, operationId);
          groupOperationRef.current = null;
        } catch (clearError) {
          uncertain = true;
          displayedError = clearError;
          if (operation) groupOperationRef.current = { ...operation, recovery: true };
        }
      } else if (uncertain && operation) {
        groupOperationRef.current = { ...operation, recovery: true };
      }
      setGroupRecovery(uncertain ? "uncertain" : "clear");
      setGroupError(errorText(displayedError, "浏览器分组操作失败，请重试。"));
    } finally {
      setGroupBusy(false);
    }
  }, [bridge, groupBusy, groupRecovery, onClose, opened, space.id, space.name, urls]);

  const remaining = urls ? pendingTargets(urls, opened).length : 0;
  const retrying = opened.length > 0;
  const close = () => {
    if (!groupBusy) onClose();
  };

  return (
    <div className="home-open-overlay" role="presentation" onClick={close}>
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
            <p
              className="home-open-browser-status"
              data-state={bridge.status}
              role="status"
            >
              {bridge.status === "checking"
                ? "正在连接浏览器分组助手…"
                : bridge.status === "available"
                  ? `浏览器已连接，将自动归入「${space.name}」分组。`
                  : "浏览器分组助手未连接，可降级为普通新标签打开。"}
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
            {groupError && (
              <p className="home-open-error" role="alert">{groupError}</p>
            )}
            {groupRecovery === "uncertain" && !groupBusy && (
              <p className="home-open-warning" role="alert">
                上次分组请求的最终状态尚未确认。请只重试“在浏览器分组打开”，
                避免普通方式与后台任务重复打开。
              </p>
            )}
            <div className="home-open-actions">
              <button
                type="button"
                className="home-open-primary"
                disabled={bridge.status !== "available" || groupBusy}
                onClick={() => void handleBrowserGroup()}
              >
                {groupBusy
                  ? <LoaderCircle className="spin" aria-hidden="true" />
                  : <FolderOpen aria-hidden="true" />}
                {groupBusy
                  ? "正在创建浏览器分组…"
                  : retrying
                    ? `分组打开剩下的 ${remaining} 个`
                    : "在浏览器分组打开"}
              </button>
              <button
                type="button"
                className="home-open-secondary"
                disabled={groupBusy || groupRecovery !== "clear"}
                onClick={() => handleOpenAll("tabs")}
              >
                {retrying ? `普通方式重试 ${remaining} 个` : "普通新标签打开"}
              </button>
              {!retrying && (
                <button
                  type="button"
                  className="home-open-secondary"
                  disabled={groupBusy || groupRecovery !== "clear"}
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
        <button type="button" className="home-open-close" disabled={groupBusy} onClick={close}>
          取消
        </button>
      </div>
    </div>
  );
}
