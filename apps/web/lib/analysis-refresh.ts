"use client";

import { useEffect, useRef } from "react";

import type { LibrarySite } from "@/lib/library-contract";

// Keep the first updates responsive, then cap the visible pending gap at 15s.
// Twenty-four attempts cover roughly five minutes without leaving a finished
// analysis looking stuck for another one or two minutes late in the run.
export const ANALYSIS_REFRESH_DELAYS_MS = [
  1_000,
  2_000,
  3_000,
  5_000,
  8_000,
  12_000,
  ...Array.from({ length: 18 }, () => 15_000),
] as const;

type AnalysisStatus = Pick<LibrarySite, "analysisStatus">;

export function hasRefreshableSiteAnalysis(sites: readonly AnalysisStatus[]): boolean {
  return sites.some(({ analysisStatus }) => (
    analysisStatus === "not_analyzed" || analysisStatus === "pending"
  ));
}

export function analysisRefreshDelay(index: number): number | null {
  return ANALYSIS_REFRESH_DELAYS_MS[index] ?? null;
}

type AnalysisRefreshRun = {
  scope: string;
  nextDelayIndex: number;
  active: boolean;
  cycle: number;
  timer: ReturnType<typeof setTimeout> | null;
};

type UseBoundedAnalysisRefreshOptions = {
  scope: string;
  enabled: boolean;
  refresh: () => void | Promise<void>;
};

/** Run at most one bounded polling sequence for each stable query scope. */
export function useBoundedAnalysisRefresh({
  scope,
  enabled,
  refresh,
}: UseBoundedAnalysisRefreshOptions): void {
  const refreshRef = useRef(refresh);
  const enabledRef = useRef(enabled);
  const runRef = useRef<AnalysisRefreshRun | null>(null);

  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);

  useEffect(() => {
    enabledRef.current = enabled;
  }, [enabled]);

  useEffect(() => {
    const run: AnalysisRefreshRun = {
      scope,
      nextDelayIndex: 0,
      active: false,
      cycle: 0,
      timer: null,
    };
    runRef.current = run;

    return () => {
      if (run.timer !== null) clearTimeout(run.timer);
      if (runRef.current === run) runRef.current = null;
    };
  }, [scope]);

  useEffect(() => {
    const run = runRef.current;
    if (!run || run.scope !== scope) return;

    if (!enabled) {
      if (run.timer !== null) {
        clearTimeout(run.timer);
        run.timer = null;
      }
      // A completed/failed list refresh can make a scope temporarily
      // ineligible.  Invalidate any in-flight callback as well, so enabling
      // the same scope later starts a fresh bounded sequence instead of being
      // blocked by the previous one.
      run.active = false;
      run.nextDelayIndex = 0;
      run.cycle += 1;
      return;
    }
    if (run.active) return;
    run.active = true;
    run.nextDelayIndex = 0;
    const cycle = run.cycle + 1;
    run.cycle = cycle;

    const scheduleNext = () => {
      const delay = analysisRefreshDelay(run.nextDelayIndex);
      if (delay === null) {
        run.timer = null;
        run.active = false;
        return;
      }
      run.nextDelayIndex += 1;
      run.timer = setTimeout(() => {
        run.timer = null;
        if (
          runRef.current !== run
          || !enabledRef.current
          || run.cycle !== cycle
        ) return;
        void Promise.resolve()
          .then(() => refreshRef.current())
          .catch(() => undefined)
          .finally(() => {
            if (
              runRef.current === run
              && enabledRef.current
              && run.cycle === cycle
            ) scheduleNext();
          });
      }, delay);
    };

    scheduleNext();
  }, [enabled, scope]);

  useEffect(() => {
    if (!enabled) return;
    let lastRefreshAt = 0;

    const refreshOnActivity = () => {
      if (!enabledRef.current || document.visibilityState === "hidden") return;
      const now = Date.now();
      // Browsers commonly dispatch visibilitychange and focus together.
      if (now - lastRefreshAt < 1_000) return;
      lastRefreshAt = now;
      void Promise.resolve()
        .then(() => refreshRef.current())
        .catch(() => undefined);
    };

    window.addEventListener("focus", refreshOnActivity);
    document.addEventListener("visibilitychange", refreshOnActivity);
    return () => {
      window.removeEventListener("focus", refreshOnActivity);
      document.removeEventListener("visibilitychange", refreshOnActivity);
    };
  }, [enabled, scope]);
}
