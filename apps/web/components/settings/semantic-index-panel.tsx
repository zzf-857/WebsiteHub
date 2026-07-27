"use client";

import { Database, LoaderCircle, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import {
  loadSemanticIndexStatus,
  rebuildSemanticIndex,
  SearchApiError,
} from "@/lib/search-client";
import type { SemanticIndexRun, SemanticIndexStatus } from "@/lib/search-contract";

function errorMessage(error: unknown): string {
  if (error instanceof SearchApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "语义索引状态读取失败";
}

function runNotice(run: SemanticIndexRun): string {
  if (run.reason === "provider_unavailable") {
    return "当前没有可用的 embedding Provider，请检查服务商配置后重试";
  }
  if (run.reason === "already_running") {
    return "已有一轮索引在进行中，本次没有重复排队";
  }
  return `已排队，本轮预计发出 ${run.estimatedRequests} 次请求${
    run.dropped ? `；已丢弃 ${run.dropped} 条旧向量` : ""
  }`;
}

export function SemanticIndexPanel() {
  const [status, setStatus] = useState<SemanticIndexStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // 「确认要花这些请求吗」的中间态。刻意不做成 window.confirm：
  // 用户得看清预估请求数才算知情，浏览器原生弹窗塞不下这些信息。
  const [pendingRebuild, setPendingRebuild] = useState<null | { dropExisting: boolean }>(null);

  // 与 provider-settings-workspace 同一写法：loader 定义在 effect 内部。
  // 抽成 useCallback 再在 effect 里调，会被 react 规则判为「effect 内同步 setState」。
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = () => setRefreshKey((current) => current + 1);

  useEffect(() => {
    const controller = new AbortController();

    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const next = await loadSemanticIndexStatus(controller.signal);
        if (controller.signal.aborted) return;
        setStatus(next);
      } catch (failure) {
        if (controller.signal.aborted) return;
        setError(errorMessage(failure));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };

    void load();
    return () => controller.abort();
  }, [refreshKey]);

  const confirmRebuild = async () => {
    if (!pendingRebuild || !status) return;
    setBusy(true);
    setError(null);
    try {
      const run = await rebuildSemanticIndex({
        dropExisting: pendingRebuild.dropExisting,
        limit: status.passLimit,
      });
      setPendingRebuild(null);
      setNotice(runNotice(run));
      refresh();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="provider-section" aria-labelledby="semantic-index-title">
      <header className="provider-section-head">
        <h2 id="semantic-index-title" className="provider-section-title">
          <Database size={16} aria-hidden="true" />
          语义检索索引
        </h2>
      </header>
      <p className="provider-page-lead">
        为资料库里的网站生成向量，搜索时按「相关度」排序才会用到。
        生成会调用你自己配置的 embedding Provider，<strong>消耗你的额度</strong>，
        所以这里只显示花费、不自动开跑。
      </p>

      {loading && !status && (
        <p className="provider-notice">
          <LoaderCircle size={14} className="spinner" aria-hidden="true" /> 正在读取索引状态…
        </p>
      )}

      {status && (
        <dl className="provider-index-stats">
          <div>
            <dt>站点总数</dt>
            <dd>{status.totalSites}</dd>
          </div>
          <div>
            <dt>已索引</dt>
            <dd>{status.indexed}</dd>
          </div>
          <div>
            <dt>{status.pendingCapped ? "本轮待索引" : "待索引"}</dt>
            <dd>
              {status.pending}
              {status.pendingCapped ? "+" : ""}
            </dd>
          </div>
          <div>
            <dt>本轮预计请求</dt>
            <dd>{status.estimatedRequests}</dd>
          </div>
        </dl>
      )}

      {status && !status.configured && (
        <p className="provider-notice">
          还没有启用 embedding 类型的 Provider。搜索会继续使用关键词匹配，
          「相关度」排序也能用，只是不带语义召回——这是正常降级，不是错误。
        </p>
      )}

      {status?.running && (
        <p className="provider-notice">
          <LoaderCircle size={14} className="spinner" aria-hidden="true" />
          正在索引中，完成后刷新即可看到最新进度。
        </p>
      )}

      {notice && <p className="provider-notice">{notice}</p>}
      {error && <p className="provider-error">{error}</p>}

      {pendingRebuild && status ? (
        <div className="provider-notice">
          {pendingRebuild.dropExisting ? (
            <p>
              即将丢弃现有向量并重建。全库预计共发出
              <strong> {status.rebuildEstimatedRequests} </strong>
              次请求；本轮处理 {status.rebuildPassSites} 个站点，预计发出
              <strong> {status.rebuildPassEstimatedRequests} </strong>
              次请求，消耗你自己的 Provider 额度。
              {status.rebuildCapped
                ? ` 单轮上限为 ${status.passLimit} 个站点，本轮完成后还需继续补齐。`
                : ""}
              确认继续？
            </p>
          ) : (
            <p>
              即将补齐缺失的向量，预计发出
              <strong> {status.estimatedRequests} </strong>
              次请求，消耗你自己的 Provider 额度。
              {status.pendingCapped
                ? ` 单轮上限为 ${status.passLimit} 个站点，本轮完成后可以再点一次。`
                : ""}
              确认继续？
            </p>
          )}
          <div className="provider-card-actions">
            <button
              className="provider-btn provider-btn-primary"
              type="button"
              onClick={() => void confirmRebuild()}
              disabled={busy}
            >
              {busy ? "排队中…" : "确认开始"}
            </button>
            <button
              className="provider-btn provider-btn-secondary"
              type="button"
              onClick={() => setPendingRebuild(null)}
              disabled={busy}
            >
              取消
            </button>
          </div>
        </div>
      ) : (
        <div className="provider-card-actions">
          <button
            className="provider-btn provider-btn-primary"
            type="button"
            onClick={() => {
              setNotice(null);
              setPendingRebuild({ dropExisting: false });
            }}
            disabled={!status?.configured || status.running || status.pending === 0}
          >
            补齐缺失索引
          </button>
          <button
            className="provider-btn provider-btn-secondary"
            type="button"
            onClick={() => {
              setNotice(null);
              setPendingRebuild({ dropExisting: true });
            }}
            disabled={!status?.configured || status.running}
          >
            全部重建
          </button>
          <button
            className="provider-btn provider-btn-secondary"
            type="button"
            onClick={refresh}
            disabled={loading}
          >
            <RefreshCw size={14} aria-hidden="true" /> 刷新
          </button>
        </div>
      )}
    </section>
  );
}
