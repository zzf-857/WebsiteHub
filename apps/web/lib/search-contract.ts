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
  running: boolean;
};

export type SemanticIndexRun = {
  // 「已排队」和「已在进行中」是两个答案：混为一谈会让用户以为自己排了两轮。
  scheduled: boolean;
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
    running: guards.boolean(payload.running, "语义索引状态.running"),
  };
}

export function normalizeSemanticIndexRun(value: unknown): SemanticIndexRun {
  const payload = guards.record(value, "语义索引任务");
  return {
    scheduled: guards.boolean(payload.scheduled, "语义索引任务.scheduled"),
    dropped: guards.count(payload.dropped, "语义索引任务.dropped"),
    estimatedRequests: guards.count(
      payload.estimated_requests,
      "语义索引任务.estimated_requests",
    ),
  };
}
