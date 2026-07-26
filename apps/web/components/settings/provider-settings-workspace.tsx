"use client";

import { AlertTriangle, Cpu, Plus, RefreshCw, Search, Waypoints, X } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState, type ComponentType } from "react";

import { Spinner } from "@/components/react-bits/spinner";
import { ProviderConfigCard } from "@/components/settings/provider-config-card";
import { ProviderDialog } from "@/components/settings/provider-dialog";
import {
  ProviderForm,
  type ProviderSubmitResult,
  type ProviderTestOutcome,
} from "@/components/settings/provider-form";
import {
  ProviderApiError,
  createProviderConfig,
  deleteProviderConfig,
  enableProviderConfig,
  listProviderConfigs,
  listProviderRegistry,
  testProviderConnection,
  updateProviderConfig,
} from "@/lib/provider-client";
import {
  ProviderContractError,
  type ProviderConfig,
  type ProviderCreateInput,
  type ProviderKind,
  type ProviderRegistryItem,
  type ProviderTestInput,
  type ProviderUpdateInput,
} from "@/lib/provider-contract";
import {
  PROVIDER_KIND_SECTIONS,
  providerFieldErrorFor,
  providerKindLabel,
  providerTestTone,
} from "@/lib/provider-form";

type DialogState =
  | { kind: "create"; providerKind: ProviderKind }
  | { kind: "edit"; config: ProviderConfig }
  | { kind: "delete"; config: ProviderConfig }
  | null;

const KIND_ICONS: Record<ProviderKind, ComponentType<{ "aria-hidden"?: boolean }>> = {
  model: Cpu,
  search: Search,
  embedding: Waypoints,
};

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

// 409/404 都意味着「本地这份数据已经过期」：继续在旧版本号上重试只会一直失败，
// 唯一正确的反应是重新拉一遍列表。
function isStaleError(error: unknown): boolean {
  return error instanceof ProviderApiError && (error.status === 409 || error.status === 404);
}

function errorText(error: unknown, fallback: string): string {
  if (error instanceof ProviderApiError || error instanceof ProviderContractError) {
    return error.message;
  }
  return fallback;
}

function submitFailure(error: unknown, fallback: string): ProviderSubmitResult {
  if (error instanceof ProviderApiError) {
    const mapped = providerFieldErrorFor(error.code, error.message);
    if (mapped) return { ok: false, field: mapped.field, message: mapped.message };
  }
  return { ok: false, message: errorText(error, fallback) };
}

export function ProviderSettingsWorkspace() {
  const [registry, setRegistry] = useState<ProviderRegistryItem[]>([]);
  const [configs, setConfigs] = useState<ProviderConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogState>(null);
  const [busyConfigId, setBusyConfigId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    const load = async () => {
      setLoading(true);
      setLoadError(null);
      try {
        // 注册表与已保存配置互不依赖，并行拉取。
        const [registryItems, configItems] = await Promise.all([
          listProviderRegistry(controller.signal),
          listProviderConfigs(undefined, controller.signal),
        ]);
        if (controller.signal.aborted) return;
        setRegistry(registryItems);
        setConfigs(configItems);
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        setLoadError(errorText(error, "服务商配置加载失败，请重试"));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };

    void load();
    return () => controller.abort();
  }, [refreshVersion]);

  // 任何写操作后都整表重拉，而不是就地打补丁：
  // 启用一个配置会让同 kind 下原先启用的那条在后端被改成停用并涨版本号，
  // 只更新被点的那张卡片会让列表显示两个「已启用」。
  const reload = useCallback(async () => {
    try {
      setConfigs(await listProviderConfigs());
      setLoadError(null);
    } catch (error) {
      setLoadError(errorText(error, "服务商配置刷新失败，请重试"));
    }
  }, []);

  const registryByProvider = useMemo(
    () => new Map(registry.map((item) => [item.provider, item])),
    [registry],
  );

  const configsByKind = useMemo(() => {
    const grouped = new Map<ProviderKind, ProviderConfig[]>();
    for (const section of PROVIDER_KIND_SECTIONS) grouped.set(section.kind, []);
    for (const config of configs) grouped.get(config.kind)?.push(config);
    return grouped;
  }, [configs]);

  const registryForKind = useCallback(
    (kind: ProviderKind) => registry.filter((item) => item.kinds.includes(kind)),
    [registry],
  );

  const closeDialog = useCallback(() => {
    setDialog(null);
    setDeleteError(null);
  }, []);

  const announceSaved = (config: ProviderConfig, verb: string) => {
    const enabledNote = config.enabled
      ? config.kind === "model"
        ? "，已启用，现在可以回首页和 Agent 对话了"
        : "，已启用"
      : "";
    setNotice(`已${verb}${providerKindLabel(config.kind)}配置“${config.displayName}”${enabledNote}`);
  };

  const handleCreate = async (input: ProviderCreateInput): Promise<ProviderSubmitResult> => {
    try {
      const created = await createProviderConfig(input);
      setDialog(null);
      announceSaved(created, "保存");
      await reload();
      return { ok: true };
    } catch (error) {
      return submitFailure(error, "保存配置失败，请稍后重试");
    }
  };

  const handleUpdate = async (
    config: ProviderConfig,
    input: ProviderUpdateInput,
  ): Promise<ProviderSubmitResult> => {
    try {
      const updated = await updateProviderConfig(config.id, input);
      setDialog(null);
      announceSaved(updated, "更新");
      await reload();
      return { ok: true };
    } catch (error) {
      if (isStaleError(error)) {
        setDialog(null);
        setNotice("该配置已被其他操作更新，已重新加载最新数据，请重新编辑");
        await reload();
      }
      return submitFailure(error, "更新配置失败，请稍后重试");
    }
  };

  const handleEnable = async (config: ProviderConfig) => {
    if (busyConfigId) return;
    setBusyConfigId(config.id);
    setNotice(null);
    try {
      const enabled = await enableProviderConfig(config.id, config.version);
      announceSaved(enabled, "启用");
    } catch (error) {
      if (isStaleError(error)) {
        setNotice("该配置已被其他操作更新，已重新加载最新数据");
      } else {
        setLoadError(errorText(error, "启用配置失败，请稍后重试"));
      }
    } finally {
      setBusyConfigId(null);
      await reload();
    }
  };

  const handleDelete = async (config: ProviderConfig) => {
    if (deleting) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteProviderConfig(config.id, config.version);
      setDialog(null);
      setNotice(`已删除配置“${config.displayName}”`);
      await reload();
    } catch (error) {
      if (isStaleError(error)) {
        setDialog(null);
        setNotice("该配置已被其他操作更新或删除，已重新加载最新数据");
        await reload();
      } else {
        setDeleteError(errorText(error, "删除配置失败，请稍后重试"));
      }
    } finally {
      setDeleting(false);
    }
  };

  const handleTest = async (input: ProviderTestInput): Promise<ProviderTestOutcome> => {
    try {
      const result = await testProviderConnection(input);
      return {
        tone: providerTestTone(result.status),
        message: result.message ?? "连接测试已完成",
      };
    } catch (error) {
      if (error instanceof ProviderApiError) {
        const retryAfter = error.retryAfterSeconds;
        return {
          tone: error.status === 429 ? "rate-limited" : "error",
          message: retryAfter === undefined
            ? error.message
            : `${error.message}（约 ${retryAfter} 秒后可再试）`,
        };
      }
      return { tone: "error", message: errorText(error, "连接测试失败，请稍后重试") };
    }
  };

  const dialogKind = dialog?.kind === "create"
    ? dialog.providerKind
    : dialog?.kind === "edit"
      ? dialog.config.kind
      : null;

  return (
    <main className="site-main">
      <header className="workspace-page-header">
        <span className="page-kicker">设置</span>
        <h1>服务商</h1>
        <p className="provider-page-lead">
          WebHub 不内置任何厂商密钥。填入你自己的 API Key 后，Agent 才会以你的额度调用模型。
          密钥加密保存在本账号名下，保存后任何界面都只显示掩码。
        </p>
      </header>

      <div className="provider-page">
        {notice && (
          <p className="provider-notice" role="status">
            <span>{notice}</span>
            <button type="button" onClick={() => setNotice(null)} aria-label="关闭提示">
              <X aria-hidden="true" />
            </button>
          </p>
        )}

        {loadError && (
          <p className="provider-error" role="alert">
            <AlertTriangle aria-hidden="true" />
            <span>{loadError}</span>
            <button
              className="provider-btn provider-btn-secondary provider-btn-sm"
              type="button"
              onClick={() => setRefreshVersion((current) => current + 1)}
            >
              <RefreshCw aria-hidden="true" />
              重新加载
            </button>
          </p>
        )}

        {loading ? (
          <p className="provider-loading" aria-busy="true">
            <Spinner size={14} />
            正在加载服务商配置
          </p>
        ) : (
          PROVIDER_KIND_SECTIONS.map((section) => {
            const Icon = KIND_ICONS[section.kind];
            const items = configsByKind.get(section.kind) ?? [];
            const vendors = registryForKind(section.kind);
            return (
              <section className="provider-section" key={section.kind} aria-label={section.title}>
                <div className="provider-section-head">
                  <div className="provider-section-titles">
                    <h2 className="provider-section-title">
                      <Icon aria-hidden={true} />
                      {section.title}
                      <span className="provider-section-count">{items.length} 个配置</span>
                    </h2>
                    <p className="provider-section-desc">{section.description}</p>
                  </div>
                  <button
                    className="provider-btn provider-btn-primary"
                    type="button"
                    disabled={vendors.length === 0}
                    onClick={() => setDialog({ kind: "create", providerKind: section.kind })}
                  >
                    <Plus aria-hidden="true" />
                    添加配置
                  </button>
                </div>

                {items.length === 0 ? (
                  <p className="provider-empty">
                    还没有{section.title}配置。
                    {section.kind === "model" && "配好一个模型服务后，首页的 Agent 才能回话。"}
                  </p>
                ) : (
                  <div className="provider-card-grid">
                    {items.map((config) => (
                      <ProviderConfigCard
                        key={config.id}
                        config={config}
                        definition={registryByProvider.get(config.provider) ?? null}
                        busy={busyConfigId === config.id}
                        onEnable={() => void handleEnable(config)}
                        onEdit={() => setDialog({ kind: "edit", config })}
                        onDelete={() => {
                          setDeleteError(null);
                          setDialog({ kind: "delete", config });
                        }}
                      />
                    ))}
                  </div>
                )}
              </section>
            );
          })
        )}

        {!loading && !loadError && configs.length === 0 && (
          <p className="provider-section-desc">
            配好模型服务后，可以回到 <Link href="/">首页</Link> 直接和 Agent 对话。
          </p>
        )}
      </div>

      <ProviderDialog
        open={dialog?.kind === "create" || dialog?.kind === "edit"}
        title={dialog?.kind === "edit" ? "编辑配置" : "添加配置"}
        description={
          dialogKind === null
            ? undefined
            : `${providerKindLabel(dialogKind)}：同一时刻只有一个配置处于启用状态。`
        }
        onClose={closeDialog}
      >
        {dialog?.kind === "create" && (
          <ProviderForm
            kind={dialog.providerKind}
            registry={registryForKind(dialog.providerKind)}
            onCancel={closeDialog}
            onCreate={handleCreate}
            onTest={handleTest}
          />
        )}
        {dialog?.kind === "edit" && (
          <ProviderForm
            // version 变化时重建表单，避免旧版本号残留在草稿里。
            key={`${dialog.config.id}:${dialog.config.version}`}
            kind={dialog.config.kind}
            registry={registryForKind(dialog.config.kind)}
            config={dialog.config}
            onCancel={closeDialog}
            onUpdate={(input) => handleUpdate(dialog.config, input)}
            onTest={handleTest}
          />
        )}
      </ProviderDialog>

      <ProviderDialog
        open={dialog?.kind === "delete"}
        title="删除服务商配置"
        busy={deleting}
        onClose={closeDialog}
      >
        {dialog?.kind === "delete" && (
          <>
            <p className="provider-dialog-text">
              将删除{providerKindLabel(dialog.config.kind)}配置“{dialog.config.displayName}”
              及其加密保存的 API Key。此操作不可撤销。
            </p>
            {dialog.config.enabled && (
              <p className="provider-dialog-text">
                这是当前启用中的配置，删除后该类型将没有可用服务，直到你启用另一个配置。
              </p>
            )}
            {deleteError && (
              <p className="provider-error" role="alert">
                <AlertTriangle aria-hidden="true" />
                <span>{deleteError}</span>
              </p>
            )}
            <div className="provider-form-actions">
              <button
                className="provider-btn provider-btn-secondary"
                type="button"
                disabled={deleting}
                onClick={closeDialog}
              >
                取消
              </button>
              <button
                className="provider-btn provider-btn-danger"
                type="button"
                disabled={deleting}
                onClick={() => void handleDelete(dialog.config)}
              >
                {deleting && <Spinner size={14} />}
                {deleting ? "正在删除" : "确认删除"}
              </button>
            </div>
          </>
        )}
      </ProviderDialog>
    </main>
  );
}
