import { createContractGuards } from "./contract-guards.ts";

// 语义索引状态。字段与后端 SemanticIndexStatusResponse 一一对应；
// estimatedRequests 是**花钱数字**，UI 必须在触发重建前把它显示出来。
export type SemanticIndexStatus = {
  configured: boolean;
  modelName: string | null;
  totalSites: number;
  indexed: number;
  pending: number;
  // 待索引数是否被单轮上限截断。true 时界面必须说「本轮」而不是「全部」。
  pendingCapped: boolean;
  estimatedRequests: number;
  rebuildEstimatedRequests: number;
  rebuildPassSites: number;
  rebuildPassEstimatedRequests: number;
  rebuildCapped: boolean;
  passLimit: number;
  running: boolean;
};

export const SEMANTIC_INDEX_RUN_REASONS = [
  "scheduled",
  "already_running",
  "provider_unavailable",
] as const;
export type SemanticIndexRunReason = (typeof SEMANTIC_INDEX_RUN_REASONS)[number];

export type SemanticIndexRun = {
  // 「已排队」和「已在进行中」是两个答案：混为一谈会让用户以为自己排了两轮。
  scheduled: boolean;
  reason: SemanticIndexRunReason;
  dropped: number;
  estimatedRequests: number;
};

export class SearchContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SearchContractError";
  }
}

const guards = createContractGuards((message) => new SearchContractError(message));

export function normalizeSemanticIndexStatus(value: unknown): SemanticIndexStatus {
  const payload = guards.record(value, "语义索引状态");
  return {
    configured: guards.boolean(payload.configured, "语义索引状态.configured"),
    modelName: guards.nullableText(payload.model_name, "语义索引状态.model_name"),
    totalSites: guards.count(payload.total_sites, "语义索引状态.total_sites"),
    indexed: guards.count(payload.indexed, "语义索引状态.indexed"),
    pending: guards.count(payload.pending, "语义索引状态.pending"),
    pendingCapped: guards.boolean(payload.pending_capped, "语义索引状态.pending_capped"),
    estimatedRequests: guards.count(
      payload.estimated_requests,
      "语义索引状态.estimated_requests",
    ),
    rebuildEstimatedRequests: guards.count(
      payload.rebuild_estimated_requests,
      "语义索引状态.rebuild_estimated_requests",
    ),
    rebuildPassSites: guards.count(
      payload.rebuild_pass_sites,
      "语义索引状态.rebuild_pass_sites",
    ),
    rebuildPassEstimatedRequests: guards.count(
      payload.rebuild_pass_estimated_requests,
      "语义索引状态.rebuild_pass_estimated_requests",
    ),
    rebuildCapped: guards.boolean(
      payload.rebuild_capped,
      "语义索引状态.rebuild_capped",
    ),
    passLimit: guards.count(payload.pass_limit, "语义索引状态.pass_limit"),
    running: guards.boolean(payload.running, "语义索引状态.running"),
  };
}

export function normalizeSemanticIndexRun(value: unknown): SemanticIndexRun {
  const payload = guards.record(value, "语义索引任务");
  const scheduled = guards.boolean(payload.scheduled, "语义索引任务.scheduled");
  const reason = guards.literal(
    payload.reason,
    "语义索引任务.reason",
    SEMANTIC_INDEX_RUN_REASONS,
  );
  if (scheduled !== (reason === "scheduled")) {
    throw new SearchContractError("语义索引任务.scheduled 与 reason 不一致");
  }
  return {
    scheduled,
    reason,
    dropped: guards.count(payload.dropped, "语义索引任务.dropped"),
    estimatedRequests: guards.count(
      payload.estimated_requests,
      "语义索引任务.estimated_requests",
    ),
  };
}
