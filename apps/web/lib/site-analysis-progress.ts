import type { LibraryAnalysisPhase } from "./library-contract.ts";

export type SiteAnalysisDisplayPhase = "queued" | LibraryAnalysisPhase;
export type SiteAnalysisStepState = "done" | "current" | "pending";

export const SITE_ANALYSIS_PROGRESS_STEPS = [
  { phase: "queued", label: "正在检查模型配置并提交分析任务" },
  { phase: "fetching_page", label: "正在读取网站公开页面" },
  { phase: "preparing_evidence", label: "正在整理页面证据与分类候选" },
  { phase: "waiting_model", label: "正在等待模型分析资源" },
  { phase: "calling_model", label: "正在补充公开资料并生成分类、标签与介绍" },
  { phase: "saving_result", label: "正在校验结果并写入网址库" },
] as const satisfies readonly Readonly<{
  phase: SiteAnalysisDisplayPhase;
  label: string;
}>[];

export function siteAnalysisProgressRows(phase: SiteAnalysisDisplayPhase) {
  const currentIndex = SITE_ANALYSIS_PROGRESS_STEPS.findIndex(
    (step) => step.phase === phase,
  );
  return SITE_ANALYSIS_PROGRESS_STEPS.map((step, index) => ({
    ...step,
    state: (
      index < currentIndex ? "done" : index === currentIndex ? "current" : "pending"
    ) as SiteAnalysisStepState,
  }));
}
