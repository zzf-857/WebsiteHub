"use client";

import { Check, DatabaseBackup, Download, FileJson, Info, Upload } from "lucide-react";
import Link from "next/link";
import { useRef, useState } from "react";

import { Spinner } from "@/components/react-bits/spinner";
import { ProviderDialog } from "@/components/settings/provider-dialog";
import { useDataPortability } from "@/components/settings/use-data-portability";
import { PORTABILITY_EXPORT_URL } from "@/lib/portability-client";
import {
  PORTABILITY_LABELS,
  PORTABILITY_SAMPLE_LABELS,
  PORTABILITY_SECTIONS,
  type PortabilityPreview,
  type PortabilityResult,
  type PortabilitySampleKey,
} from "@/lib/portability-contract";

function PreviewSummary({ preview }: { preview: PortabilityPreview }) {
  return (
    <div className="data-preview">
      <div className="data-section-heading">
        <div>
          <span className="data-eyebrow">导入前预览</span>
          <h3>确认这次恢复的内容</h3>
        </div>
        <span className="data-badge">尚未写入</span>
      </div>
      <p className="data-caption">已有收藏仅补充空白资料并合并标签，保留现有名称、分类和置顶。</p>
      <div className="data-table-scroll" tabIndex={0} aria-label="导入变更统计">
        <table className="data-table">
          <thead><tr><th scope="col">内容</th><th scope="col">新增</th><th scope="col">合并 / 复用</th><th scope="col">跳过</th></tr></thead>
          <tbody>{PORTABILITY_SECTIONS.map((section) => {
            const count = preview.counts[section];
            return <tr key={section}><th scope="row">{PORTABILITY_LABELS[section]}</th><td>{count.create}</td><td>{count.merge + count.reuse}</td><td>{count.skipped}</td></tr>;
          })}</tbody>
        </table>
      </div>
      <p className="data-caption">站内对话仅供导出留存，当前备份版本不会恢复对话。</p>
      {preview.warnings.length > 0 && (
        <div className="data-warnings" role="note">
          <h4><Info aria-hidden="true" />需要留意</h4>
          <ul>{preview.warnings.map((warning, index) => <li key={`${warning.code}-${index}`}>{warning.message}</li>)}</ul>
        </div>
      )}
      <div className="data-samples">
        {(Object.keys(PORTABILITY_SAMPLE_LABELS) as PortabilitySampleKey[]).map((key) => {
          const samples = preview.samples[key];
          if (!samples?.length) return null;
          return (
            <details key={key} className="data-sample-group">
              <summary>{PORTABILITY_SAMPLE_LABELS[key]}<span>前 {samples.length} 条示例</span></summary>
              <ul>{samples.map((sample, index) => (
                <li key={`${key}-${index}`}><strong>{sample.name}</strong>{sample.url && <span>{sample.url}</span>}</li>
              ))}</ul>
            </details>
          );
        })}
      </div>
      <p className="data-caption">预览保留至 {new Date(preview.expiresAt).toLocaleString("zh-CN", { hour12: false })}，过期后请重新上传。</p>
    </div>
  );
}

function ImportResult({ result }: { result: PortabilityResult }) {
  return (
    <div className="data-result">
      <div className="data-result-mark"><Check aria-hidden="true" /></div>
      <h3>备份已恢复</h3>
      <p className="data-caption">内容已合并到当前账号，可以前往收藏库继续整理。</p>
      <div className="data-table-scroll" tabIndex={0} aria-label="导入结果统计">
        <table className="data-table">
          <thead><tr><th scope="col">内容</th><th scope="col">已新增</th><th scope="col">已合并 / 复用</th></tr></thead>
          <tbody>{PORTABILITY_SECTIONS.filter((section) => section !== "conversations").map((section) => (
            <tr key={section}><th scope="row">{PORTABILITY_LABELS[section]}</th><td>{result.created[section]}</td><td>{result.merged[section]}</td></tr>
          ))}</tbody>
        </table>
      </div>
      <Link className="data-button data-button-primary" href="/library">去收藏库查看</Link>
    </div>
  );
}

export function DataPortabilityWorkspace() {
  const state = useDataPortability();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const hasPreview = state.preview !== null && state.phase !== "result";
  const canRetry = state.file !== null && !state.busy;

  return (
    <main className="data-workspace">
      <header className="data-page-heading">
        <div className="data-title-icon"><DatabaseBackup aria-hidden="true" /></div>
        <div><p className="data-eyebrow">账号数据</p><h1>数据导出与恢复</h1><p>为你的收藏留一份备份，也可以把它带到另一个 WebHub 账号。</p></div>
      </header>
      <div className="data-columns">
        <section className="data-card data-export" aria-labelledby="data-export-title">
          <span className="data-eyebrow">01 / 导出</span>
          <h2 id="data-export-title">带走你的收藏</h2>
          <p>下载当前账号的 WebHub JSON 备份，保存分类、标签与收藏之间的关系。</p>
          <dl className="data-inclusions">
            <div><dt>包含</dt><dd>收藏及使用记录、分类、标签、Space、订阅源和站内对话正文。</dd></div>
            <div><dt>不包含</dt><dd>服务商配置与密钥、登录会话、Agent 令牌及审计、书签原文件与暂存、媒体缓存、对话推理及工具原始输出。</dd></div>
          </dl>
          <a className={`data-button ${hasPreview || state.result ? "data-button-secondary" : "data-button-primary"}`} href={PORTABILITY_EXPORT_URL} download><Download aria-hidden="true" />下载备份</a>
          <p className="data-caption">备份包含你的私人收藏和对话，请保存在可信的位置。</p>
        </section>
        <section className="data-card data-import" aria-labelledby="data-import-title" aria-busy={state.busy}>
          <span className="data-eyebrow">02 / 恢复</span>
          <h2 id="data-import-title">从备份恢复</h2>
          <p>上传后先预览变更，确认导入才会写入当前账号。</p>
          {state.phase !== "result" && (
            <div
              className="data-dropzone"
              data-dragging={dragging || undefined}
              onDragOver={(event) => { event.preventDefault(); if (!state.busy) setDragging(true); }}
              onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false); }}
              onDrop={(event) => { event.preventDefault(); setDragging(false); state.selectFile(event.dataTransfer.files[0]); }}
            >
              {state.file ? <FileJson aria-hidden="true" /> : <Upload aria-hidden="true" />}
              <strong>{state.file?.name ?? "选择或拖放备份文件"}</strong>
              <span className="data-caption">WebHub JSON 备份 · 最大 64 MiB</span>
              <input
                ref={inputRef}
                className="data-file-input"
                type="file"
                accept=".json,application/json"
                aria-label="选择 JSON 备份文件"
                disabled={state.busy}
                onChange={(event) => { state.selectFile(event.target.files?.[0]); event.target.value = ""; }}
              />
              <button className="data-button data-button-secondary" type="button" disabled={state.busy} onClick={() => inputRef.current?.click()}>{state.file ? "重新选择" : "选择备份文件"}</button>
            </div>
          )}
          {state.error && (
            <div className="data-error" role="alert">
              <p>{state.error}</p>
              {canRetry && <button className="data-button data-button-secondary" type="button" onClick={() => {
                if (hasPreview && !state.invalidPreview) state.setConfirming(true);
                else void state.upload();
              }}>{state.invalidPreview ? "重新上传" : "重试"}</button>}
              {!state.file && <p className="data-caption">重新选择文件后可再次上传。</p>}
            </div>
          )}
          {state.busy && (
            <div className="data-progress" role="status" aria-live="polite">
              <Spinner size={20} /><div><strong>{state.phase === "uploading" ? "正在上传并检查备份…" : "正在恢复备份…"}</strong><p>{state.phase === "uploading" ? "文件较大时可能需要一点时间，完成后会显示预览。" : "正在合并资料，请保持页面打开。"}</p></div>
            </div>
          )}
          {hasPreview && state.preview && <PreviewSummary preview={state.preview} />}
          {state.result && <ImportResult result={state.result} />}
          {state.phase === "idle" && state.file && !state.error && <button className="data-button data-button-primary" type="button" onClick={() => void state.upload()}><Upload aria-hidden="true" />上传并预览</button>}
          {hasPreview && !state.invalidPreview && <button className="data-button data-button-primary" type="button" disabled={state.busy} onClick={() => state.setConfirming(true)}>确认导入</button>}
          {state.phase === "idle" && !state.file && !state.error && <p className="data-empty">还没有选择备份。可先下载左侧备份，或选择此前导出的文件。</p>}
        </section>
      </div>
      <ProviderDialog open={state.confirming} title="确认恢复这份备份？" onClose={() => state.setConfirming(false)}>
        <p className="data-confirm-copy">这会把预览中的内容合并到当前账号。已有收藏只补充空白资料，导入后不能一键撤销。建议先下载当前账号备份。</p>
        <div className="data-dialog-actions">
          <button className="data-button data-button-secondary" type="button" onClick={() => state.setConfirming(false)}>取消</button>
          <button className="data-button data-button-primary" type="button" onClick={() => void state.apply()}>确认恢复</button>
        </div>
      </ProviderDialog>
    </main>
  );
}
