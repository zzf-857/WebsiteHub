"use client";

import {
  Check,
  CircleAlert,
  RefreshCw,
  WandSparkles,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { errorText, isAbortError } from "@/components/home/home-shared";
import { CountUp } from "@/components/react-bits/count-up";
import { Spinner } from "@/components/react-bits/spinner";
import {
  getActiveMetadataBackfill,
  getMetadataBackfillProgress,
  LibraryApiError,
  startMetadataBackfill,
} from "@/lib/library-client";
import {
  isMetadataBackfillTerminalStatus,
  type MetadataBackfillProgress,
} from "@/lib/library-contract";

// 前台的更新足够及时，同时始终只保留一条 GET 在飞；后台页降到低频，
// 避免用户切走后继续给浏览器、后端和目标站点制造无意义的请求压力。
const FOREGROUND_POLL_DELAY_MS = 2_500;
const BACKGROUND_POLL_DELAY_MS = 15_000;

type ToolbarState =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "running"; progress: MetadataBackfillProgress }
  | { kind: "completed"; progress: MetadataBackfillProgress }
  | { kind: "warning"; message: string; progress: MetadataBackfillProgress }
  | { kind: "failed"; message: string; progress: MetadataBackfillProgress }
  | { kind: "error"; message: string; progress?: MetadataBackfillProgress };

type HomeMetadataBackfillToolbarProps = {
  /** Called once for each terminal run so surrounding homepage data can refresh. */
  onCompleted?: () => void;
};

function pollDelay(): number {
  return typeof document !== "undefined" && document.hidden
    ? BACKGROUND_POLL_DELAY_MS
    : FOREGROUND_POLL_DELAY_MS;
}

function progressPercent(progress: MetadataBackfillProgress): number {
  if (isMetadataBackfillTerminalStatus(progress.status)) return 100;
  if (progress.totalCount === 0) return 0;
  return Math.floor((progress.completedCount / progress.totalCount) * 100);
}

function progressSummary(progress: MetadataBackfillProgress): string {
  if (progress.status === "completed") {
    return progress.totalCount === 0
      ? "没有需要补全的网站"
      : `已完成 ${progress.completedCount} / ${progress.totalCount}`;
  }
  if (isMetadataBackfillTerminalStatus(progress.status)) {
    const prefix = progress.stoppedEarly
      ? "任务已提前停止"
      : progress.status === "failed"
        ? "任务失败"
        : "处理结束，存在未补全项";
    return `${prefix} ${progress.completedCount} / ${progress.totalCount}`;
  }
  if (progress.runningCount > 0) {
    return `已处理 ${progress.completedCount} / ${progress.totalCount}，${progress.runningCount} 个正在处理`;
  }
  return `已处理 ${progress.completedCount} / ${progress.totalCount}，等待补全`;
}

function progressDetail(progress: MetadataBackfillProgress): string {
  const outcomes = [
    `完成 ${progress.completeCount}`,
    `受限 ${progress.limitedCount}`,
    `失败 ${progress.failedCount}`,
  ];
  if (progress.skippedCount > 0) outcomes.push(`跳过 ${progress.skippedCount}`);
  if (!isMetadataBackfillTerminalStatus(progress.status) && progress.queuedCount > 0) {
    outcomes.push(`等待 ${progress.queuedCount}`);
  }
  return outcomes.join(" · ");
}

function terminalMessage(progress: MetadataBackfillProgress): string {
  if (progress.stoppedEarly) {
    return "任务已提前停止，请检查模型或搜索 Provider 配置、搜索服务是否支持批量、密钥权限或模型工具调用支持后重试。";
  }
  if (progress.status === "failed") {
    return "任务执行失败，请检查模型或搜索 Provider 配置、搜索服务是否支持批量、密钥权限或模型工具调用支持后重试。";
  }
  return "部分网站未能补全，可再次补全失败或受限的网站。";
}

/**
 * Account-wide LLM website-enrichment command for the homepage.
 *
 * Polling is deliberately timeout-chained instead of interval-based: a slow
 * response must finish before the next request can start, so foreground
 * interaction never creates overlapping progress reads.
 */
export function HomeMetadataBackfillToolbar({
  onCompleted,
}: Readonly<HomeMetadataBackfillToolbarProps>) {
  const [state, setState] = useState<ToolbarState>({ kind: "idle" });
  const [pollRevision, setPollRevision] = useState(0);
  const onCompletedRef = useRef(onCompleted);
  const finishedRunRef = useRef<string | null>(null);
  const startControllerRef = useRef<AbortController | null>(null);
  const startingRef = useRef(false);

  useEffect(() => {
    onCompletedRef.current = onCompleted;
  }, [onCompleted]);

  const finishRun = useCallback((progress: MetadataBackfillProgress) => {
    if (progress.status === "completed") {
      setState({ kind: "completed", progress });
    } else if (progress.status === "failed") {
      setState({ kind: "failed", progress, message: terminalMessage(progress) });
    } else {
      setState({ kind: "warning", progress, message: terminalMessage(progress) });
    }
    if (finishedRunRef.current === progress.runId) return;
    finishedRunRef.current = progress.runId;
    onCompletedRef.current?.();
  }, []);

  // A visibility transition invalidates the currently scheduled wait (or
  // in-flight read) and lets the tracking effect choose the appropriate rate.
  useEffect(() => {
    const handleVisibilityChange = () => {
      setPollRevision((revision) => revision + 1);
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, []);

  useEffect(() => () => {
    startControllerRef.current?.abort();
  }, []);

  // A run belongs to the account, not this React tree. Reattach after a page
  // refresh without posting another command or asking the user to remember an
  // opaque run id.
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const progress = await getActiveMetadataBackfill(controller.signal);
        if (controller.signal.aborted || progress === null) return;
        setState((current) => current.kind === "idle"
          ? { kind: "running", progress }
          : current);
      } catch (error) {
        if (controller.signal.aborted || isAbortError(error)) return;
        setState((current) => current.kind === "idle"
          ? { kind: "error", message: errorText(error, "补全任务状态读取失败，请重新开始") }
          : current);
      }
    })();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (state.kind !== "running") return;

    const controller = new AbortController();
    const runId = state.progress.runId;
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const progress = await getMetadataBackfillProgress(runId, controller.signal);
          if (controller.signal.aborted) return;
          if (progress.runId !== runId) {
            setState({
              kind: "error",
              message: "补全任务标识不一致，请重新开始",
            });
            return;
          }
          if (isMetadataBackfillTerminalStatus(progress.status)) {
            finishRun(progress);
            return;
          }
          setState({ kind: "running", progress });
        } catch (error) {
          if (controller.signal.aborted || isAbortError(error)) return;
          if (
            error instanceof LibraryApiError &&
            error.status === 404 &&
            error.code === "metadata_backfill_not_found"
          ) {
            setState({
              kind: "error",
              message: "补全任务已不存在，请重新开始",
            });
          } else {
            setState({
              kind: "error",
              message: errorText(error, "补全进度读取失败，请重试"),
              progress: state.progress,
            });
          }
        }
      })();
    }, pollDelay());

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [finishRun, pollRevision, state]);

  const start = useCallback(async () => {
    if (startingRef.current || state.kind === "running") return;
    startingRef.current = true;
    startControllerRef.current?.abort();
    const controller = new AbortController();
    startControllerRef.current = controller;
    setState({ kind: "starting" });
    try {
      const progress = await startMetadataBackfill(controller.signal);
      if (controller.signal.aborted) return;
      if (isMetadataBackfillTerminalStatus(progress.status)) {
        finishRun(progress);
      } else {
        setState({ kind: "running", progress });
      }
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error)) return;
      setState({ kind: "error", message: errorText(error, "补全任务启动失败，请重试") });
    } finally {
      if (startControllerRef.current === controller) startControllerRef.current = null;
      startingRef.current = false;
    }
  }, [finishRun, state.kind]);

  const resume = useCallback(() => {
    if (state.kind !== "error" || !state.progress) {
      void start();
      return;
    }
    setState({ kind: "running", progress: state.progress });
  }, [start, state]);

  const progress = state.kind === "running" ||
    state.kind === "completed" ||
    state.kind === "warning" ||
    state.kind === "failed"
    ? state.progress
    : state.kind === "error"
      ? state.progress
      : undefined;
  const busy = state.kind === "starting" || state.kind === "running";
  const hasResumableProgress = state.kind === "error" && state.progress !== undefined;
  const actionLabel = state.kind === "starting"
    ? "正在启动"
    : state.kind === "running"
      ? "正在补全"
      : state.kind === "completed"
        ? "再次补全"
        : state.kind === "warning" || state.kind === "failed"
          ? "再次补全"
          : hasResumableProgress
            ? "重试查询"
            : state.kind === "error"
              ? "重新开始"
              : "开始补全";

  return (
    <section
      className="home-metadata-backfill"
      data-state={state.kind}
      aria-label="LLM 网站资料批量分析"
    >
      <div className="home-metadata-backfill-overview">
        <span className="home-metadata-backfill-icon" aria-hidden="true">
          <WandSparkles />
        </span>
        <div className="home-metadata-backfill-copy">
          <h2>LLM 网站分析</h2>
          <p>批量补全分类、标签、简介、详细介绍、图标和预览图</p>
        </div>
      </div>

      <div className="home-metadata-backfill-action-wrap">
        <button
          type="button"
          className="home-metadata-backfill-action"
          onClick={() => {
            if (hasResumableProgress) resume();
            else void start();
          }}
          disabled={busy}
          title={actionLabel}
        >
          {state.kind === "starting" || state.kind === "running" ? (
            <Spinner />
          ) : state.kind === "completed" ? (
            <Check aria-hidden="true" />
          ) : state.kind === "warning" || state.kind === "failed" || state.kind === "error" ? (
            <RefreshCw aria-hidden="true" />
          ) : (
            <WandSparkles aria-hidden="true" />
          )}
          {actionLabel}
        </button>
      </div>

      {progress && (
        <div
          className="home-metadata-backfill-progress"
          aria-live={
            state.kind === "completed" || state.kind === "warning" || state.kind === "failed"
              ? "polite"
              : "off"
          }
        >
          <div className="home-metadata-backfill-progress-head">
            <span>{progressSummary(progress)}</span>
            <strong><CountUp value={progressPercent(progress)} />%</strong>
          </div>
          {progress.totalCount > 0 && (
            <div
              className="home-metadata-backfill-progress-track"
              role="progressbar"
              aria-label="网站信息补全进度"
              aria-valuemin={0}
              aria-valuemax={progress.totalCount}
              aria-valuenow={Math.min(progress.completedCount, progress.totalCount)}
              aria-valuetext={progressSummary(progress)}
            >
              <span style={{ width: `${progressPercent(progress)}%` }} />
            </div>
          )}
          <p>{progressDetail(progress)}</p>
        </div>
      )}

      {state.kind === "error" && (
        <p className="home-metadata-backfill-error" data-tone="error" role="alert">
          <CircleAlert aria-hidden="true" />
          {state.message}
        </p>
      )}

      {state.kind === "warning" && (
        <p className="home-metadata-backfill-error" data-tone="warning" role="status">
          <CircleAlert aria-hidden="true" />
          {state.message}
        </p>
      )}

      {state.kind === "failed" && (
        <p className="home-metadata-backfill-error" data-tone="error" role="alert">
          <CircleAlert aria-hidden="true" />
          {state.message}
        </p>
      )}
    </section>
  );
}
