"use client";

import { ExternalLink, PlugZap } from "lucide-react";
import {
  useId,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { Spinner } from "@/components/react-bits/spinner";
import {
  SECRET_MASK,
  type ProviderConfig,
  type ProviderCreateInput,
  type ProviderKind,
  type ProviderRegistryItem,
  type ProviderTestInput,
  type ProviderUpdateInput,
} from "@/lib/provider-contract";
import {
  buildProviderCreateInput,
  buildProviderUpdateInput,
  createProviderDraft,
  editProviderDraft,
  hasProviderDraftError,
  providerTestTone,
  validateProviderDraft,
  type ProviderDraft,
  type ProviderDraftErrors,
  type ProviderDraftField,
  type ProviderSecretIntent,
  type ProviderValidationMode,
} from "@/lib/provider-form";

export type ProviderSubmitResult =
  | { ok: true }
  | { ok: false; field?: ProviderDraftField; message: string };

export type ProviderTestOutcome = {
  tone: ReturnType<typeof providerTestTone>;
  message: string;
  /** 测试成功时从厂商目录读到的模型名，用于模型名输入框的下拉候选 */
  models: string[];
};

type ProviderFormProps = {
  kind: ProviderKind;
  /** 已按 kind 过滤过的注册表条目 */
  registry: ProviderRegistryItem[];
  /** 传入表示编辑态；不传表示新建 */
  config?: ProviderConfig;
  onCancel: () => void;
  onCreate?: (input: ProviderCreateInput) => Promise<ProviderSubmitResult>;
  onUpdate?: (input: ProviderUpdateInput) => Promise<ProviderSubmitResult>;
  onTest: (input: ProviderTestInput) => Promise<ProviderTestOutcome>;
};

export function ProviderForm({
  kind,
  registry,
  config,
  onCancel,
  onCreate,
  onUpdate,
  onTest,
}: Readonly<ProviderFormProps>) {
  const editing = config !== undefined;
  const [draft, setDraft] = useState<ProviderDraft>(() =>
    config ? editProviderDraft(config) : createProviderDraft(registry.length === 1 ? registry[0].provider : ""),
  );
  const [errors, setErrors] = useState<ProviderDraftErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ProviderTestOutcome | null>(null);
  // 拉取到的模型列表独立于 testResult 保存：改动别的字段会清掉结果条，
  // 但已经拉到的候选没必要跟着一起消失。
  const [fetchedModels, setFetchedModels] = useState<string[]>([]);

  // 明文密钥只存在于这个 DOM 节点里：不进 React state、不进 props、不进任何日志。
  // 组件只保留「填没填」这个布尔量来驱动提示与校验。
  const secretRef = useRef<HTMLInputElement>(null);
  const [secretFilled, setSecretFilled] = useState(false);
  const [clearSecret, setClearSecret] = useState(false);

  const vendorGroupId = useId();
  const modelListId = useId();
  const displayNameId = useId();
  const baseUrlId = useId();
  const modelNameId = useId();
  const secretId = useId();
  const enabledId = useId();

  const definition = useMemo(
    () => registry.find((item) => item.provider === draft.provider) ?? null,
    [draft.provider, registry],
  );

  const secretIntent: ProviderSecretIntent = clearSecret
    ? "clear"
    : secretFilled
      ? "write"
      : "keep";

  const readSecret = (): string => secretRef.current?.value ?? "";

  const clearSecretInput = () => {
    if (secretRef.current) secretRef.current.value = "";
    setSecretFilled(false);
  };

  const patchDraft = (patch: Partial<ProviderDraft>, clearedField?: ProviderDraftField) => {
    setDraft((current) => ({ ...current, ...patch }));
    setTestResult(null);
    if (clearedField) {
      setErrors((current) => {
        if (!current[clearedField]) return current;
        const next = { ...current };
        delete next[clearedField];
        return next;
      });
    }
  };

  const selectVendor = (item: ProviderRegistryItem) => {
    patchDraft(
      {
        provider: item.provider,
        // 首次选择时用厂商名做配置名的默认值，省掉一次输入；已经改过就不覆盖。
        displayName: draft.displayName.trim() ? draft.displayName : item.label,
      },
      "provider",
    );
    setErrors((current) => ({ ...current, provider: undefined, displayName: undefined }));
  };

  // 注册表九宫格是单选组：漫游 tabindex + 方向键，保证只用键盘也能选厂商。
  const handleVendorKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const keys = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    const last = registry.length - 1;
    let next = index;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") next = index === last ? 0 : index + 1;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = index === 0 ? last : index - 1;
    else if (event.key === "Home") next = 0;
    else next = last;
    selectVendor(registry[next]);
    const group = event.currentTarget.parentElement;
    group?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
  };

  const validate = (mode: ProviderValidationMode = "save"): ProviderDraftErrors => {
    const nextErrors = validateProviderDraft({
      kind,
      definition,
      draft,
      secretIntent,
      hasStoredSecret: config?.hasSecret ?? false,
      mode,
    });
    setErrors(nextErrors);
    return nextErrors;
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    setFormError(null);
    setTestResult(null);

    // 先做本地校验再读密钥：字段填错时不会白白清掉用户刚粘贴的 Key。
    if (hasProviderDraftError(validate())) return;

    setBusy(true);
    try {
      let result: ProviderSubmitResult;
      if (editing && config && onUpdate) {
        const input = buildProviderUpdateInput({ config, draft, secretIntent, secret: readSecret() });
        if (input === null) {
          setFormError("没有需要保存的修改");
          return;
        }
        result = await onUpdate(input);
      } else if (onCreate) {
        result = await onCreate(buildProviderCreateInput({ kind, draft, secret: readSecret() }));
      } else {
        return;
      }

      if (result.ok) {
        // 只在成功后清空密钥输入框：失败时保留，用户不必重新粘贴。
        clearSecretInput();
        return;
      }
      const field = result.field;
      if (field) setErrors((current) => ({ ...current, [field]: result.message }));
      else setFormError(result.message);
    } finally {
      setBusy(false);
    }
  };

  const handleTest = async () => {
    if (testing || busy) return;
    setFormError(null);
    // 测试只需要「够得着厂商」的字段：配置名、模型名都还没定也能先拉列表。
    if (hasProviderDraftError(validate("test"))) return;

    setTesting(true);
    try {
      const secret = readSecret().trim();
      const baseUrl = draft.baseUrl.trim() || null;
      const modelName = draft.modelName.trim() || null;
      // 编辑态且没重新填 Key 时，只能让后端用已保存的密文去测；
      // 其余情况都测「此刻表单里的参数」，不碰已保存的配置。
      const input: ProviderTestInput =
        editing && config && !secret && !clearSecret
          ? { configId: config.id, expectedVersion: config.version }
          : {
            kind,
            provider: draft.provider,
            baseUrl,
            modelName,
            ...(secret ? { secret } : {}),
          };
      const outcome = await onTest(input);
      setTestResult(outcome);
      if (outcome.models.length > 0) setFetchedModels(outcome.models);
    } finally {
      setTesting(false);
    }
  };

  const secretHint = editing
    ? clearSecret
      ? "保存后将删除已存储的 API Key。"
      : config?.hasSecret
        ? `已存储一个 API Key（显示为 ${SECRET_MASK}）。留空表示保留原密钥。`
        : "当前未存储 API Key。"
    : definition?.secretRequired
      ? "该服务商必须提供 API Key。密钥加密存储，之后任何界面都只会显示掩码。"
      : "该服务商无需 API Key，可以直接留空。";

  const showModelName = kind !== "search";
  const submitting = busy;

  return (
    <form className="provider-form" onSubmit={(event) => void handleSubmit(event)} noValidate>
      {editing ? (
        <div className="provider-form-field provider-form-field--full">
          <span className="provider-form-label">服务商</span>
          <p className="provider-form-hint">
            {definition?.label ?? config?.provider}（创建后不可更改，需要换厂商请新建配置）
          </p>
        </div>
      ) : (
        <>
          <div className="provider-form-field provider-form-field--full">
            <span className="provider-form-label" id={vendorGroupId}>选择服务商</span>
            {errors.provider && <p className="provider-form-error" role="alert">{errors.provider}</p>}
          </div>
          <div className="provider-registry-grid" role="radiogroup" aria-labelledby={vendorGroupId}>
            {registry.map((item, index) => {
              const selected = item.provider === draft.provider;
              return (
                <button
                  key={item.provider}
                  className="provider-registry-item"
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  data-selected={selected}
                  tabIndex={selected || (!draft.provider && index === 0) ? 0 : -1}
                  disabled={submitting}
                  onClick={() => selectVendor(item)}
                  onKeyDown={(event) => handleVendorKeyDown(event, index)}
                >
                  <span>{item.label}</span>
                  <span className="provider-registry-item-note">
                    {item.secretRequired ? "需要 API Key" : "无需 API Key"}
                    {item.baseUrlRequired ? " · 需填地址" : ""}
                  </span>
                </button>
              );
            })}
          </div>
        </>
      )}

      <div className="provider-form-field">
        <label className="provider-form-label" htmlFor={displayNameId}>配置名称</label>
        <input
          id={displayNameId}
          className="provider-input"
          type="text"
          maxLength={80}
          value={draft.displayName}
          disabled={submitting}
          placeholder="例如：日常对话"
          aria-invalid={errors.displayName ? true : undefined}
          onChange={(event) => patchDraft({ displayName: event.target.value }, "displayName")}
        />
        {errors.displayName && <p className="provider-form-error" role="alert">{errors.displayName}</p>}
      </div>

      {showModelName && (
        <div className="provider-form-field">
          <label className="provider-form-label" htmlFor={modelNameId}>
            模型名称
            {!draft.enabled && <span className="provider-form-optional"> · 启用前必填</span>}
          </label>
          {/* 双模式：datalist 让同一个控件既能下拉选已拉取的模型，也能直接手填
              服务商刚上线、还没出现在目录里的模型名。 */}
          <input
            id={modelNameId}
            className="provider-input"
            type="text"
            maxLength={160}
            value={draft.modelName}
            disabled={submitting}
            list={fetchedModels.length > 0 ? modelListId : undefined}
            placeholder={
              fetchedModels.length > 0 ? "下拉选择，或直接手填" : "填写服务商文档里的模型标识"
            }
            aria-invalid={errors.modelName ? true : undefined}
            onChange={(event) => patchDraft({ modelName: event.target.value }, "modelName")}
          />
          {fetchedModels.length > 0 && (
            <>
              <datalist id={modelListId}>
                {fetchedModels.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
              <p className="provider-form-hint">
                已从服务商读取 {fetchedModels.length} 个模型，点开输入框可下拉选择，也可以继续手填。
              </p>
            </>
          )}
          {errors.modelName && <p className="provider-form-error" role="alert">{errors.modelName}</p>}
        </div>
      )}

      {definition && (
        <div className="provider-form-field provider-form-field--full">
          <label className="provider-form-label" htmlFor={baseUrlId}>
            Base URL
            {!definition.baseUrlRequired && <span className="provider-form-optional"> · 可选</span>}
          </label>
          <input
            id={baseUrlId}
            className="provider-input"
            type="url"
            maxLength={2048}
            value={draft.baseUrl}
            disabled={submitting}
            placeholder={definition.allowsPrivateBaseUrl ? "http://127.0.0.1:11434" : "https://api.example.com/v1"}
            aria-invalid={errors.baseUrl ? true : undefined}
            onChange={(event) => patchDraft({ baseUrl: event.target.value }, "baseUrl")}
          />
          <p className="provider-form-hint">
            {definition.baseUrlRequired
              ? definition.allowsPrivateBaseUrl
                ? "本地服务填写自身监听地址即可。"
                : "必须是 HTTPS，且不能指向本机或局域网地址。"
              : "留空则使用该服务商的默认地址。"}
          </p>
          {errors.baseUrl && <p className="provider-form-error" role="alert">{errors.baseUrl}</p>}
        </div>
      )}

      {definition && (
        <div className="provider-form-field provider-form-field--full">
          <label className="provider-form-label" htmlFor={secretId}>
            API Key
            {!definition.secretRequired && <span className="provider-form-optional"> · 可选</span>}
          </label>
          <input
            id={secretId}
            ref={secretRef}
            className="provider-input provider-input--secret"
            type="password"
            autoComplete="off"
            spellCheck={false}
            maxLength={8192}
            disabled={submitting || clearSecret}
            placeholder={editing && config?.hasSecret ? "留空则保留原密钥" : "粘贴服务商控制台里的 API Key"}
            aria-invalid={errors.secret ? true : undefined}
            onChange={(event) => {
              setSecretFilled(event.target.value.length > 0);
              setTestResult(null);
              setErrors((current) => ({ ...current, secret: undefined }));
            }}
          />
          <p className="provider-form-hint">{secretHint}</p>
          {editing && config?.hasSecret && (
            <label className="provider-form-hint">
              <input
                type="checkbox"
                checked={clearSecret}
                disabled={submitting}
                onChange={(event) => {
                  setClearSecret(event.target.checked);
                  if (event.target.checked) clearSecretInput();
                  setTestResult(null);
                  setErrors((current) => ({ ...current, secret: undefined }));
                }}
              />
              {" "}删除已存储的 API Key
            </label>
          )}
          {definition.applicationUrl && (
            <a
              className="provider-apply-link"
              href={definition.applicationUrl}
              target="_blank"
              rel="noopener noreferrer"
              referrerPolicy="no-referrer"
            >
              去 {definition.label} 申请密钥
              <ExternalLink aria-hidden="true" />
            </a>
          )}
          {errors.secret && <p className="provider-form-error" role="alert">{errors.secret}</p>}
        </div>
      )}

      <div className="provider-form-field provider-form-field--full">
        <label className="provider-form-hint" htmlFor={enabledId}>
          <input
            id={enabledId}
            type="checkbox"
            checked={draft.enabled}
            disabled={submitting}
            onChange={(event) => patchDraft({ enabled: event.target.checked })}
          />
          {" "}保存后立即启用（同类型下原先启用的配置会自动停用）
        </label>
      </div>

      {formError && <p className="provider-error provider-form-field--full" role="alert">{formError}</p>}

      {testResult && (
        <p
          className="provider-test-result provider-form-field--full"
          data-status={testResult.tone}
          role="status"
        >
          {testResult.message}
        </p>
      )}

      <div className="provider-form-actions">
        {definition?.connectionTestSupported && (
          <button
            className="provider-btn provider-btn-secondary"
            type="button"
            disabled={submitting || testing}
            onClick={() => void handleTest()}
          >
            {testing ? <Spinner size={14} /> : <PlugZap aria-hidden="true" />}
            {testing ? "正在测试" : "测试连接"}
          </button>
        )}
        <button
          className="provider-btn provider-btn-secondary"
          type="button"
          disabled={submitting}
          onClick={onCancel}
        >
          取消
        </button>
        <button className="provider-btn provider-btn-primary" type="submit" disabled={submitting}>
          {submitting && <Spinner size={14} />}
          {submitting ? "正在保存" : editing ? "保存修改" : "保存配置"}
        </button>
      </div>
    </form>
  );
}
