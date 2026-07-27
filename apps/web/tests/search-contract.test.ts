import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SearchContractError,
  normalizeSemanticIndexRun,
  normalizeSemanticIndexStatus,
} from "../lib/search-contract.ts";

const STATUS = {
  configured: true,
  model_name: "embed-1",
  total_sites: 2024,
  indexed: 1500,
  pending: 512,
  pending_capped: true,
  estimated_requests: 9,
  rebuild_estimated_requests: 32,
  rebuild_pass_sites: 512,
  rebuild_pass_estimated_requests: 8,
  rebuild_capped: true,
  pass_limit: 512,
  running: false,
};

test("索引状态按后端字段名归一化", () => {
  assert.deepEqual(normalizeSemanticIndexStatus(STATUS), {
    configured: true,
    modelName: "embed-1",
    totalSites: 2024,
    indexed: 1500,
    pending: 512,
    pendingCapped: true,
    estimatedRequests: 9,
    rebuildEstimatedRequests: 32,
    rebuildPassSites: 512,
    rebuildPassEstimatedRequests: 8,
    rebuildCapped: true,
    passLimit: 512,
    running: false,
  });
});

test("未配 Provider 时 model_name 为 null 是合法的", () => {
  const status = normalizeSemanticIndexStatus({
    ...STATUS,
    configured: false,
    model_name: null,
    indexed: 0,
    pending: 0,
    pending_capped: false,
    estimated_requests: 0,
  });
  assert.equal(status.configured, false);
  assert.equal(status.modelName, null);
  assert.equal(status.estimatedRequests, 0);
});

test("缺少花钱字段必须报错而不是当 0 处理", () => {
  // 静默填 0 会让确认弹窗显示「预计 0 次请求」，用户据此点了确认，
  // 实际却发出几十次——这种沉默比报错危险得多。
  const withoutEstimate: Record<string, unknown> = { ...STATUS };
  delete withoutEstimate.estimated_requests;
  assert.throws(
    () => normalizeSemanticIndexStatus(withoutEstimate),
    SearchContractError,
  );
});

test("负数或非整数的计数被拒绝", () => {
  assert.throws(() => normalizeSemanticIndexStatus({ ...STATUS, pending: -1 }), SearchContractError);
  assert.throws(
    () => normalizeSemanticIndexStatus({ ...STATUS, indexed: 1.5 }),
    SearchContractError,
  );
});

test("任务响应保留 scheduled 的真假区分", () => {
  assert.deepEqual(
    normalizeSemanticIndexRun({
      scheduled: false,
      reason: "provider_unavailable",
      dropped: 0,
      estimated_requests: 0,
    }),
    {
      scheduled: false,
      reason: "provider_unavailable",
      dropped: 0,
      estimatedRequests: 0,
    },
  );
  assert.deepEqual(
    normalizeSemanticIndexRun({
      scheduled: true,
      reason: "scheduled",
      dropped: 12,
      estimated_requests: 3,
    }),
    { scheduled: true, reason: "scheduled", dropped: 12, estimatedRequests: 3 },
  );
});

test("scheduled 不接受真值转换", () => {
  // "false" 这种字符串按真值判断会变成 true，那样界面会说「已排队」，
  // 而后端其实拒绝了这次请求。
  assert.throws(
    () => normalizeSemanticIndexRun({
      scheduled: "false",
      reason: "already_running",
      dropped: 0,
      estimated_requests: 0,
    }),
    SearchContractError,
  );
});

test("任务布尔值与原因不一致时拒绝响应", () => {
  assert.throws(
    () => normalizeSemanticIndexRun({
      scheduled: false,
      reason: "scheduled",
      dropped: 0,
      estimated_requests: 0,
    }),
    SearchContractError,
  );
});
