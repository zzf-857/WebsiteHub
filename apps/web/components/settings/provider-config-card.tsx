"use client";

import { Check, Pencil, Power, Trash2 } from "lucide-react";
import type { ReactNode } from "react";

import { Spinner } from "@/components/react-bits/spinner";
import type { ProviderConfig, ProviderRegistryItem } from "@/lib/provider-contract";

function formatMoment(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function MetaRow({ label, children }: Readonly<{ label: string; children: ReactNode }>) {
  return (
    <div className="provider-card-meta-row">
      <dt className="provider-card-meta-label">{label}</dt>
      <dd className="provider-card-meta-value">{children}</dd>
    </div>
  );
}

export function ProviderConfigCard({
  config,
  definition,
  busy,
  onEnable,
  onEdit,
  onDelete,
}: Readonly<{
  config: ProviderConfig;
  definition: ProviderRegistryItem | null;
  busy: boolean;
  onEnable: () => void;
  onEdit: () => void;
  onDelete: () => void;
}>) {
  return (
    <article className="provider-card" data-enabled={config.enabled} aria-busy={busy || undefined}>
      <div className="provider-card-head">
        <h3 className="provider-card-name" title={config.displayName}>{config.displayName}</h3>
        <div className="provider-card-badges">
          <span className="provider-badge">{definition?.label ?? config.provider}</span>
          {config.enabled ? (
            <span className="provider-badge" data-tone="enabled">
              <Check aria-hidden="true" />
              已启用
            </span>
          ) : (
            <span className="provider-badge" data-tone="muted">未启用</span>
          )}
        </div>
      </div>

      <dl className="provider-card-meta">
        {config.kind !== "search" && (
          <MetaRow label="模型">{config.modelName ?? "未填写"}</MetaRow>
        )}
        <MetaRow label="地址">{config.baseUrl ?? "服务商默认地址"}</MetaRow>
        <MetaRow label="API Key">
          {config.hasSecret ? (
            // 全站只展示后端回传的掩码，绝不还原明文。
            <span className="provider-secret-mask">{config.secretMask}</span>
          ) : (
            "未设置"
          )}
        </MetaRow>
      </dl>

      <span className="provider-card-time">更新于 {formatMoment(config.updatedAt)}</span>

      <div className="provider-card-actions">
        {!config.enabled && (
          <button
            className="provider-btn provider-btn-secondary provider-btn-sm"
            type="button"
            disabled={busy}
            onClick={onEnable}
          >
            {busy ? <Spinner size={13} /> : <Power aria-hidden="true" />}
            启用
          </button>
        )}
        <button
          className="provider-btn provider-btn-secondary provider-btn-sm"
          type="button"
          disabled={busy}
          onClick={onEdit}
        >
          <Pencil aria-hidden="true" />
          编辑
        </button>
        <span className="provider-card-actions-spacer" />
        <button
          className="provider-btn provider-btn-danger-ghost provider-btn-sm"
          type="button"
          disabled={busy}
          onClick={onDelete}
          aria-label={`删除配置 ${config.displayName}`}
        >
          <Trash2 aria-hidden="true" />
          删除
        </button>
      </div>
    </article>
  );
}
