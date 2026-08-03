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
  getMetadataBackfillPlan,
  getMetadataBackfillProgress,
  LibraryApiError,
  startMetadataBackfill,
} from "@/lib/library-client";
import {
  isMetadataBackfillTerminalStatus,
  METADATA_BACKFILL_LIMITS,
  type MetadataBackfillMode,
  type MetadataBackfillPlan,
  type MetadataBackfillProgress,
} from "@/lib/library-contract";

// 前台的更新足够及时，同时始终只保留一条 GET 在飞；后台页降到低频，
// 避免用户切走后继续给浏览器、后端和目标站点制造无意义的请求压力。
const FOREGROUND_POLL_DELAY_MS = 2_500;
const BACKGROUND_POLL_DELAY_MS = 15_000;
const PLAN_DEBOUNCE_MS = 200;

const MODE_OPTIONS: ReadonlyArray<Readonly<{
  value: MetadataBackfillMode;
  label: string;
  title: string;
}>> = [
  { value: "metadata", label: "快速补全", title: "仅补全网页元数据，不调用模型" },
  { value: "full", label: "LLM 完整分析", title: "补全分类、标签、简介和详细介绍" },
];

type ToolbarState =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "running"; progress: MetadataBackfillProgress }
  | { kind: "completed"; progress: MetadataBackfillProgress }
  | { kind: "warning"; message: string; progress: MetadataBackfillProgress }
  | { kind: "failed"; message: string; progress: MetadataBackfillProgress }
  | { kind: "error"; message: string; progress?: MetadataBackfillProgress };

type PlanState =
  | { kind: "loading" }
  | { kind: "ready"; plan: MetadataBackfillPlan }
  | { kind: "error"; message: string };

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

function retryTime(value: string | null): string | null {
  if (value === null) return null;
  const retryAt = new Date(value);
  if (retryAt.getTime() <= Date.now()) return null;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(retryAt);
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
  if (progress.providerRetryAt !== null) {
    return `已处理 ${progress.completedCount} / ${progress.totalCount}，等待模型服务恢复`;
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
  const retryAt = retryTime(progress.providerRetryAt);
  if (!isMetadataBackfillTerminalStatus(progress.status) && retryAt !== null) {
    outcomes.push(`预计 ${retryAt} 重试`);
  }
  return outcomes.join(" · ");
}

function terminalMessage(progress: MetadataBackfillProgress): string {
  const retryAt = retryTime(progress.providerRetryAt);
  if (progress.stopReason === "provider_rate_limited") {
    return retryAt === null
      ? "模型服务触发限流，任务已安全停止，已完成结果已保留。请稍后再次补全。"
      : `模型服务触发限流，任务已安全停止，已完成结果已保留。建议在 ${retryAt} 后再次补全。`;
  }
  if (progress.stopReason === "provider_temporary_failure") {
    return retryAt === null
      ? "模型服务暂时不可用，任务已安全停止，已完成结果已保留。请稍后重试。"
      : `模型服务暂时不可用，任务已安全停止，已完成结果已保留。建议在 ${retryAt} 后重试。`;
  }
  if (progress.stopReason === "provider_unavailable") {
    return "当前模型 Provider 不可用，任务已安全停止，已完成结果已保留。请检查启用状态、密钥和模型配置后重试。";
  }
  if (progress.stopReason === "internal_error") {
    return "任务因内部错误停止，已完成结果已保留。请稍后重试；若持续发生，请检查 API 日志。";
  }
  if (progress.stoppedEarly) {
    return "任务已提前停止，已完成结果已保留。请检查模型或搜索 Provider、密钥权限和模型工具调用支持后重试。";
  }
  if (progress.status === "failed") {
    return "任务执行失败，已完成结果已保留。请检查模型或搜索 Provider、密钥权限和模型工具调用支持后重试。";
  }
  return "部分网站未能补全，可再次补全失败或受限的网站。";
}

function modeLabel(mode: MetadataBackfillMode): string {
  return mode === "metadata" ? "快速补全" : "LLM 完整分析";
}

/**
 * Account-wide website-enrichment command for the homepage.
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
  const [planRevision, setPlanRevision] = useState(0);
  const [mode, setMode] = useState<MetadataBackfillMode>("metadata");
  const [limits, setLimits] = useState<Record<MetadataBackfillMode, number>>({
    metadata: METADATA_BACKFILL_LIMITS.metadata.defaultLimit,
    full: METADATA_BACKFILL_LIMITS.full.defaultLimit,
  });
  const [planState, setPlanState] = useState<PlanState>({ kind: "loading" });
  const onCompletedRef = useRef(onCompleted);
  const finishedRunRef = useRef<string | null>(null);
  const startControllerRef = useRef<AbortController | null>(null);
  const startingRef = useRef(false);

  const progress = state.kind === "running" ||
    state.kind === "completed" ||
    state.kind === "warning" ||
    state.kind === "failed"
    ? state.progress
    : state.kind === "error"
      ? state.progress
      : undefined;
  const busy = state.kind === "starting" || state.kind === "running";
  const progressIsActive = progress !== undefined &&
    !isMetadataBackfillTerminalStatus(progress.status);
  const controlsLocked = busy || progressIsActive;
  const hasResumableProgress = state.kind === "error" && progressIsActive;
  const limit = limits[mode];
  const plan = planState.kind === "ready" &&
    planState.plan.mode === mode &&
    planState.plan.requestedLimit === limit
    ? planState.plan
    : null;

  useEffect(() => {
    onCompletedRef.current = onCompleted;
  }, [onCompleted]);

  const finishRun = useCallback((nextProgress: MetadataBackfillProgress) => {
    if (nextProgress.status === "completed") {
      setState({ kind: "completed", progress: nextProgress });
    } else if (nextProgress.status === "failed") {
      setState({
        kind: "failed",
        progress: nextProgress,
        message: terminalMessage(nextProgress),
      });
    } else {
      setState({
        kind: "warning",
        progress: nextProgress,
        message: terminalMessage(nextProgress),
      });
    }
    if (finishedRunRef.current === nextProgress.runId) return;
    finishedRunRef.current = nextProgress.runId;
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
        const nextProgress = await getActiveMetadataBackfill(controller.signal);
        if (controller.signal.aborted || nextProgress === null) return;
        setMode(nextProgress.mode);
        setState((current) => current.kind === "idle"
          ? { kind: "running", progress: nextProgress }
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

  // Plans are cheap, read-only snapshots. Debounce numeric edits and reject a
  // stale response by aborting it whenever mode, limit, or active-run state changes.
  useEffect(() => {
    if (controlsLocked) return;

    const controller = new AbortController();
    setPlanState({ kind: "loading" });
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const nextPlan = await getMetadataBackfillPlan({ mode, limit }, controller.signal);
          if (controller.signal.aborted) return;
          setPlanState({ kind: "ready", plan: nextPlan });
        } catch (error) {
          if (controller.signal.aborted || isAbortError(error)) return;
          setPlanState({
            kind: "error",
            message: errorText(error, "补全范围预估失败，请重试"),
          });
        }
      })();
    }, PLAN_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [controlsLocked, limit, mode, planRevision]);

  useEffect(() => {
    if (state.kind !== "running") return;

    const controller = new AbortController();
    const runId = state.progress.runId;
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const nextProgress = await getMetadataBackfillProgress(runId, controller.signal);
          if (controller.signal.aborted) return;
          if (nextProgress.runId !== runId) {
            setState({
              kind: "error",
              message: "补全任务标识不一致，请重新开始",
            });
            return;
          }
          if (isMetadataBackfillTerminalStatus(nextProgress.status)) {
            finishRun(nextProgress);
            return;
          }
          setState({ kind: "running", progress: nextProgress });
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
    if (startingRef.current || state.kind === "running" || plan === null) return;
    if (plan.selectedCount === 0) return;
    startingRef.current = true;
    startControllerRef.current?.abort();
    const controller = new AbortController();
    startControllerRef.current = controller;
    setState({ kind: "starting" });
    try {
      const nextProgress = await startMetadataBackfill({ mode, limit }, controller.signal);
      if (controller.signal.aborted) return;
      setMode(nextProgress.mode);
      if (isMetadataBackfillTerminalStatus(nextProgress.status)) {
        finishRun(nextProgress);
      } else {
        setState({ kind: "running", progress: nextProgress });
      }
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error)) return;
      setState({ kind: "error", message: errorText(error, "补全任务启动失败，请重试") });
    } finally {
      if (startControllerRef.current === controller) startControllerRef.current = null;
      startingRef.current = false;
    }
  }, [finishRun, limit, mode, plan, state.kind]);

  const resume = useCallback(() => {
    if (state.kind !== "error" || !state.progress) {
      void start();
      return;
    }
    setState({ kind: "running", progress: state.progress });
  }, [start, state]);

  const planLoading = !controlsLocked && planState.kind === "loading";
  const planFailed = !controlsLocked && planState.kind === "error";
  const noWork = plan !== null && plan.selectedCount === 0;
  const actionLabel = state.kind === "starting"
    ? "正在启动"
    : state.kind === "running"
      ? "正在补全"
      : hasResumableProgress
        ? "重试查询"
        : planLoading
          ? "正在预估"
          : planFailed
            ? "重试预估"
            : noWork
              ? "无需补全"
              : state.kind === "completed" || state.kind === "warning" || state.kind === "failed"
                ? "再次补全"
                : state.kind === "error"
                  ? "重新开始"
                  : "开始补全";
  const actionDisabled = busy || (
    !hasResumableProgress &&
    !planFailed &&
    (planLoading || noWork || plan === null)
  );

  return (
    <section
      className="home-metadata-backfill"
      data-state={state.kind}
      aria-label="网站资料批量分析"
    >
      <div className="home-metadata-backfill-overview">
        <span className="home-metadata-backfill-icon" aria-hidden="true">
          <WandSparkles />
        </span>
        <div className="home-metadata-backfill-copy">
          <h2>网站信息分析</h2>
          <p>先补图标与预览，或按需使用 LLM 完整补全</p>
        </div>
      </div>

      <div className="home-metadata-backfill-action-wrap">
        <button
          type="button"
          className="home-metadata-backfill-action"
          onClick={() => {
            if (hasResumableProgress) resume();
            else if (planFailed) setPlanRevision((revision) => revision + 1);
            else void start();
          }}
          disabled={actionDisabled}
          title={actionLabel}
        >
          {state.kind === "starting" || state.kind === "running" || planLoading ? (
            <Spinner />
          ) : planFailed ? (
            <RefreshCw aria-hidden="true" />
          ) : state.kind === "completed" ? (
            <Check aria-hidden="true" />
          ) : state.kind === "warning" ||
            state.kind === "failed" ||
            state.kind === "error" ? (
              <RefreshCw aria-hidden="true" />
            ) : (
              <WandSparkles aria-hidden="true" />
            )}
          {actionLabel}
        </button>
      </div>

      <div className="home-metadata-backfill-config">
        <div className="home-metadata-backfill-modes" role="group" aria-label="网站分析模式">
          {MODE_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              aria-pressed={mode === option.value}
              title={option.title}
              disabled={controlsLocked}
              onClick={() => setMode(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>

        <label className="home-metadata-backfill-limit">
          <span>处理上限</span>
          <input
            type="number"
            inputMode="numeric"
            min={1}
            max={METADATA_BACKFILL_LIMITS[mode].maxLimit}
            value={limit}
            disabled={controlsLocked}
            onChange={(event) => {
              const value = event.currentTarget.valueAsNumber;
              if (!Number.isFinite(value)) return;
              const nextLimit = Math.min(
                METADATA_BACKFILL_LIMITS[mode].maxLimit,
                Math.max(1, Math.trunc(value)),
              );
              setLimits((current) => ({ ...current, [mode]: nextLimit }));
            }}
          />
        </label>

        <div
          className="home-metadata-backfill-plan"
          data-tone={planState.kind === "error" && !controlsLocked ? "error" : undefined}
          aria-live="polite"
        >
          {state.kind === "starting" ? (
            <><Spinner />正在建立固定任务</>
          ) : progressIsActive && progress ? (
            <>
              本次已锁定 <strong>{progress.totalCount}</strong> 个网站 · {modeLabel(progress.mode)}
            </>
          ) : planState.kind === "loading" ? (
            <><Spinner />正在计算可补全范围</>
          ) : planState.kind === "error" ? (
            <><CircleAlert aria-hidden="true" />{planState.message}</>
          ) : plan === null ? (
            "正在更新补全范围"
          ) : (
            <>
              将处理 <strong>{plan.selectedCount}</strong> / {plan.eligibleCount} 个网站 ·
              {plan.mode === "metadata" ? (
                " 不调用 LLM"
              ) : (
                <> 其中 <strong>{plan.llmCount}</strong> 个网站使用 LLM</>
              )}
            </>
          )}
        </div>
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
