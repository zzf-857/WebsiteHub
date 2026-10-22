"use client";

import {
  Activity,
  AlertTriangle,
  Cable,
  Check,
  Copy,
  KeyRound,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react";

import { Spinner } from "@/components/react-bits/spinner";
import { ProviderDialog } from "@/components/settings/provider-dialog";
import {
  AgentAccessApiError,
  createAgentToken,
  listAgentActivity,
  listAgentTokens,
  revokeAgentToken,
} from "@/lib/agent-access-client";
import {
  AGENT_TOKEN_NAME_MAX,
  AgentAccessContractError,
  agentAccessLabel,
  agentTokenDisplayPrefix,
  agentToolLabel,
  formatAgentMoment,
  genericMcpConfigSnippet,
  hermesConfigSnippet,
  listedAgentToken,
  mcpEndpointUrl,
  type AgentAccessLevel,
  type AgentActivity,
  type AgentToken,
  type CreatedAgentToken,
} from "@/lib/agent-access-contract";

const ACTIVITY_LIMIT = 50;
const ORIGIN_PLACEHOLDER = "http://<WebHub 地址>:3100";

function subscribeNothing(): () => void {
  return () => {};
}
const COPY_FEEDBACK_MS = 1_500;

type DialogState =
  | { kind: "create" }
  | { kind: "created"; token: CreatedAgentToken }
  | { kind: "revoke"; token: AgentToken }
  | null;

function errorText(error: unknown, fallback: string): string {
  if (error instanceof AgentAccessApiError || error instanceof AgentAccessContractError) {
    return error.message;
  }
  return fallback;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

// 复制反馈按 key 区分：同一页上有令牌、两段配置片段三处复制按钮。
function useCopyFeedback() {
  const [copied, setCopied] = useState<{ key: string; ok: boolean } | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);

  const copy = useCallback(async (key: string, value: string) => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    try {
      await navigator.clipboard.writeText(value);
      setCopied({ key, ok: true });
    } catch {
      setCopied({ key, ok: false });
    }
    timerRef.current = window.setTimeout(() => {
      setCopied(null);
      timerRef.current = null;
    }, COPY_FEEDBACK_MS);
  }, []);

  return { copied, copy };
}

function CopyButton({
  label,
  copiedState,
  onCopy,
}: Readonly<{ label: string; copiedState: boolean | null; onCopy: () => void }>) {
  return (
    <button
      className="provider-btn provider-btn-secondary provider-btn-sm agent-copy-btn"
      type="button"
      onClick={onCopy}
      aria-label={label}
    >
      {copiedState === true ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      <span aria-live="polite">
        {copiedState === true ? "已复制" : copiedState === false ? "复制失败" : "复制"}
      </span>
    </button>
  );
}

function ConfigSnippet({
  title,
  hint,
  value,
  copyKey,
  copied,
  onCopy,
}: Readonly<{
  title: string;
  hint: string;
  value: string;
  copyKey: string;
  copied: { key: string; ok: boolean } | null;
  onCopy: (key: string, value: string) => void;
}>) {
  const titleId = useId();
  return (
    <figure className="agent-snippet" aria-labelledby={titleId}>
      <figcaption className="agent-snippet-head">
        <span className="agent-snippet-titles">
          <span className="agent-snippet-title" id={titleId}>{title}</span>
          <span className="agent-snippet-hint">{hint}</span>
        </span>
        <CopyButton
          label={`复制${title}配置`}
          copiedState={copied?.key === copyKey ? copied.ok : null}
          onCopy={() => onCopy(copyKey, value)}
        />
      </figcaption>
      <pre className="agent-snippet-code" tabIndex={0}><code>{value}</code></pre>
    </figure>
  );
}

function CreateTokenForm({
  busy,
  error,
  onCancel,
  onSubmit,
}: Readonly<{
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onSubmit: (name: string, access: AgentAccessLevel) => void;
}>) {
  const [name, setName] = useState("");
  const [access, setAccess] = useState<AgentAccessLevel>("read-write");
  const nameId = useId();
  const hintId = useId();

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSubmit(name, access);
  };

  return (
    <form className="agent-create-form" onSubmit={submit} noValidate>
      <div className="provider-form-field">
        <label className="provider-form-label" htmlFor={nameId}>名称</label>
        <input
          className="provider-input"
          id={nameId}
          value={name}
          maxLength={AGENT_TOKEN_NAME_MAX}
          placeholder="例如 Hermes、grokbot"
          autoComplete="off"
          aria-describedby={hintId}
          disabled={busy}
          onChange={(event) => setName(event.target.value)}
        />
        <p className="provider-form-hint" id={hintId}>用来区分是哪个 Agent 在使用，最多 {AGENT_TOKEN_NAME_MAX} 个字。</p>
      </div>

      <fieldset className="agent-scope-group" disabled={busy}>
        <legend className="provider-form-label">权限</legend>
        <label className="agent-scope-option">
          <input
            type="radio"
            name="agent-token-access"
            value="read-write"
            checked={access === "read-write"}
            onChange={() => setAccess("read-write")}
          />
          <span className="agent-scope-mark" aria-hidden="true" />
          <span className="agent-scope-copy">
            <span className="agent-scope-name">读写</span>
            <span className="agent-scope-desc">
              检索、查看，并可收藏、加减标签、改分类、星标和标记阅读状态。不能删除任何收藏。
            </span>
          </span>
        </label>
        <label className="agent-scope-option">
          <input
            type="radio"
            name="agent-token-access"
            value="read"
            checked={access === "read"}
            onChange={() => setAccess("read")}
          />
          <span className="agent-scope-mark" aria-hidden="true" />
          <span className="agent-scope-copy">
            <span className="agent-scope-name">只读</span>
            <span className="agent-scope-desc">只能检索和查看收藏，适合只做问答的 Agent。</span>
          </span>
        </label>
      </fieldset>

      {error && (
        <p className="provider-error" role="alert">
          <AlertTriangle aria-hidden="true" />
          <span>{error}</span>
        </p>
      )}

      <div className="provider-form-actions">
        <button
          className="provider-btn provider-btn-secondary"
          type="button"
          disabled={busy}
          onClick={onCancel}
        >
          取消
        </button>
        <button className="provider-btn provider-btn-primary" type="submit" disabled={busy}>
          {busy && <Spinner size={14} />}
          {busy ? "正在创建" : "创建令牌"}
        </button>
      </div>
    </form>
  );
}

export function AgentAccessWorkspace() {
  const [tokens, setTokens] = useState<AgentToken[]>([]);
  const [maxActive, setMaxActive] = useState(20);
  const [activity, setActivity] = useState<AgentActivity[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [dialog, setDialog] = useState<DialogState>(null);
  const [submitting, setSubmitting] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  // 刚创建的令牌明文只留在本页内存里，用来预填配置片段；吊销、离开页面即消失，
  // 不写入任何存储。
  const [freshToken, setFreshToken] = useState<CreatedAgentToken | null>(null);
  // 同源代理地址取自当前页面；服务端渲染时给占位符，水合后换成真实地址。
  const origin = useSyncExternalStore(
    subscribeNothing,
    () => window.location.origin,
    () => ORIGIN_PLACEHOLDER,
  );
  const { copied, copy } = useCopyFeedback();

  useEffect(() => {
    const controller = new AbortController();

    const load = async () => {
      setLoading(true);
      setLoadError(null);
      try {
        // 令牌与活动互不依赖，并行拉取。
        const [tokenList, activityItems] = await Promise.all([
          listAgentTokens(controller.signal),
          listAgentActivity(ACTIVITY_LIMIT, controller.signal),
        ]);
        if (controller.signal.aborted) return;
        setTokens(tokenList.items);
        setMaxActive(tokenList.maxActive);
        setActivity(activityItems);
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        setLoadError(errorText(error, "加载 Agent 接入信息失败，请稍后重试"));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };

    void load();
    return () => controller.abort();
  }, [refreshVersion]);

  const reload = () => setRefreshVersion((current) => current + 1);

  const closeDialog = () => {
    if (submitting) return;
    setDialog(null);
    setDialogError(null);
  };

  const handleCreate = async (name: string, access: AgentAccessLevel) => {
    setSubmitting(true);
    setDialogError(null);
    try {
      const created = await createAgentToken({ name, access });
      setFreshToken(created);
      const listed = listedAgentToken(created);
      setTokens((current) => [listed, ...current.filter((item) => item.id !== created.id)]);
      setDialog({ kind: "created", token: created });
    } catch (error) {
      setDialogError(errorText(error, "创建令牌失败，请稍后重试"));
    } finally {
      setSubmitting(false);
    }
  };

  const handleRevoke = async (token: AgentToken) => {
    setSubmitting(true);
    setDialogError(null);
    try {
      await revokeAgentToken(token.id);
      setTokens((current) => current.filter((item) => item.id !== token.id));
      if (freshToken?.id === token.id) setFreshToken(null);
      setDialog(null);
      setNotice(`已吊销令牌“${token.name}”，使用它的 Agent 已无法再访问。`);
    } catch (error) {
      if (error instanceof AgentAccessApiError && error.status === 404) {
        setDialog(null);
        setNotice("该令牌已不存在，已重新加载列表");
        reload();
      } else {
        setDialogError(errorText(error, "吊销令牌失败，请稍后重试"));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const endpoint = mcpEndpointUrl(origin);
  const snippetToken = freshToken?.token ?? null;
  const atLimit = tokens.length >= maxActive;

  return (
    <main className="site-main">
      <header className="workspace-page-header">
        <span className="page-kicker">设置</span>
        <h1>Agent 接入</h1>
        <p className="provider-page-lead">
          通过 MCP（Model Context Protocol），Hermes、grokbot 等你自己的 Agent 可以用令牌检索、收藏和整理这里的资源。
          令牌只在创建时显示一次、可随时吊销；Agent 不能删除任何收藏，每次写入都会记录在下方。
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
              onClick={reload}
            >
              <RefreshCw aria-hidden="true" />
              重新加载
            </button>
          </p>
        )}

        <section className="provider-section" aria-labelledby="agent-tokens-title">
          <div className="provider-section-head">
            <div className="provider-section-titles">
              <h2 className="provider-section-title" id="agent-tokens-title">
                <KeyRound aria-hidden="true" />
                访问令牌
                {!loading && (
                  <span className="provider-section-count">{tokens.length} / {maxActive}</span>
                )}
              </h2>
              <p className="provider-section-desc">每个 Agent 用一个令牌，方便单独吊销和查看它做过什么。</p>
            </div>
            <button
              className="provider-btn provider-btn-primary"
              type="button"
              disabled={loading || atLimit}
              title={atLimit ? `最多同时保留 ${maxActive} 个有效令牌` : undefined}
              onClick={() => {
                setDialogError(null);
                setDialog({ kind: "create" });
              }}
            >
              <Plus aria-hidden="true" />
              创建令牌
            </button>
          </div>

          {loading ? (
            <p className="provider-loading" aria-busy="true">
              <Spinner size={14} />
              正在加载令牌
            </p>
          ) : tokens.length === 0 ? (
            <p className="provider-empty">还没有令牌。创建一个，把它填进 Agent 的 MCP 配置即可开始使用。</p>
          ) : (
            <ul className="agent-token-list">
              {tokens.map((token) => (
                <li className="agent-token-row" key={token.id}>
                  <div className="agent-token-main">
                    <span className="agent-token-name" title={token.name}>{token.name}</span>
                    <code className="agent-token-prefix">{agentTokenDisplayPrefix(token)}</code>
                    <span className="provider-badge" data-tone={token.canWrite ? "enabled" : undefined}>
                      {agentAccessLabel(token)}
                    </span>
                  </div>
                  <span className="agent-token-meta">
                    <span>创建于 {formatAgentMoment(token.createdAt)}</span>
                    <span>
                      {token.lastUsedAt === null
                        ? "从未使用"
                        : `最近使用 ${formatAgentMoment(token.lastUsedAt)}`}
                    </span>
                  </span>
                  <button
                    className="provider-btn provider-btn-danger-ghost provider-btn-sm agent-token-revoke"
                    type="button"
                    aria-label={`吊销令牌 ${token.name}`}
                    onClick={() => {
                      setDialogError(null);
                      setDialog({ kind: "revoke", token });
                    }}
                  >
                    <Trash2 aria-hidden="true" />
                    吊销
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="provider-section" aria-labelledby="agent-config-title">
          <div className="provider-section-titles">
            <h2 className="provider-section-title" id="agent-config-title">
              <Cable aria-hidden="true" />
              连接配置
            </h2>
            <p className="provider-section-desc">
              {snippetToken
                ? `已填入刚创建的令牌“${freshToken?.name}”。这是唯一一次能复制完整令牌的机会。`
                : "把片段里的 <令牌> 换成创建时复制的完整令牌。"}
              Agent 运行在另一台机器上时，把地址换成它能访问到的主机（例如 Tailscale 主机名）。
            </p>
          </div>
          <div className="agent-endpoint">
            <span className="agent-endpoint-label">MCP 地址</span>
            <code className="agent-endpoint-url">{endpoint}</code>
            <CopyButton
              label="复制 MCP 地址"
              copiedState={copied?.key === "endpoint" ? copied.ok : null}
              onCopy={() => void copy("endpoint", endpoint)}
            />
          </div>
          <div className="agent-snippet-grid">
            <ConfigSnippet
              title="Hermes"
              hint="~/.hermes/config.yaml"
              value={hermesConfigSnippet({ url: endpoint, token: snippetToken })}
              copyKey="hermes"
              copied={copied}
              onCopy={(key, value) => void copy(key, value)}
            />
            <ConfigSnippet
              title="其他 MCP 客户端"
              hint="Streamable HTTP · JSON"
              value={genericMcpConfigSnippet({ url: endpoint, token: snippetToken })}
              copyKey="generic"
              copied={copied}
              onCopy={(key, value) => void copy(key, value)}
            />
          </div>
        </section>

        <section className="provider-section" aria-labelledby="agent-activity-title">
          <div className="provider-section-head">
            <div className="provider-section-titles">
              <h2 className="provider-section-title" id="agent-activity-title">
                <Activity aria-hidden="true" />
                Agent 活动
              </h2>
              <p className="provider-section-desc">最近 {ACTIVITY_LIMIT} 次写入。检索和查看不会记录。</p>
            </div>
            <button
              className="provider-btn provider-btn-secondary provider-btn-sm"
              type="button"
              disabled={loading}
              onClick={reload}
            >
              <RefreshCw aria-hidden="true" />
              刷新
            </button>
          </div>
          {loading ? null : activity.length === 0 ? (
            <p className="provider-empty">还没有 Agent 写入记录。</p>
          ) : (
            <ol className="agent-activity-list">
              {activity.map((item) => (
                <li className="agent-activity-row" key={item.id}>
                  <time className="agent-activity-time" dateTime={item.createdAt}>
                    {formatAgentMoment(item.createdAt)}
                  </time>
                  <span className="agent-activity-token" title={item.tokenName}>{item.tokenName}</span>
                  <span className="provider-badge agent-activity-tool" title={item.tool}>
                    {agentToolLabel(item.tool)}
                  </span>
                  <span className="agent-activity-summary">{item.summary}</span>
                </li>
              ))}
            </ol>
          )}
        </section>
      </div>

      <ProviderDialog
        open={dialog?.kind === "create"}
        title="创建访问令牌"
        description="令牌只会显示一次，请在创建后立刻复制到 Agent 的配置里。"
        busy={submitting}
        onClose={closeDialog}
      >
        {dialog?.kind === "create" && (
          <CreateTokenForm
            busy={submitting}
            error={dialogError}
            onCancel={closeDialog}
            onSubmit={(name, access) => void handleCreate(name, access)}
          />
        )}
      </ProviderDialog>

      <ProviderDialog open={dialog?.kind === "created"} title="令牌已创建" onClose={closeDialog}>
        {dialog?.kind === "created" && (
          <>
            <p className="agent-once-warning" role="note">
              <AlertTriangle aria-hidden="true" />
              <span>这是唯一一次显示完整令牌，关闭后无法再次查看。丢失了就吊销它再建一个。</span>
            </p>
            <div className="agent-secret">
              <code className="agent-secret-value" tabIndex={0}>{dialog.token.token}</code>
              <CopyButton
                label="复制完整令牌"
                copiedState={copied?.key === "token" ? copied.ok : null}
                onCopy={() => void copy("token", dialog.token.token)}
              />
            </div>
            <p className="provider-dialog-text">
              “{dialog.token.name}”（{agentAccessLabel(dialog.token)}）已生效。页面下方的配置片段已经填好这个令牌。
            </p>
            <div className="provider-form-actions">
              <button className="provider-btn provider-btn-primary" type="button" onClick={closeDialog}>
                完成
              </button>
            </div>
          </>
        )}
      </ProviderDialog>

      <ProviderDialog
        open={dialog?.kind === "revoke"}
        title="吊销令牌"
        busy={submitting}
        onClose={closeDialog}
      >
        {dialog?.kind === "revoke" && (
          <>
            <p className="provider-dialog-text">
              吊销“{dialog.token.name}”（{agentTokenDisplayPrefix(dialog.token)}）后，使用它的 Agent
              会立即无法访问。已有的活动记录会保留。此操作不可撤销。
            </p>
            {dialogError && (
              <p className="provider-error" role="alert">
                <AlertTriangle aria-hidden="true" />
                <span>{dialogError}</span>
              </p>
            )}
            <div className="provider-form-actions">
              <button
                className="provider-btn provider-btn-secondary"
                type="button"
                disabled={submitting}
                onClick={closeDialog}
              >
                取消
              </button>
              <button
                className="provider-btn provider-btn-danger"
                type="button"
                disabled={submitting}
                onClick={() => void handleRevoke(dialog.token)}
              >
                {submitting && <Spinner size={14} />}
                {submitting ? "正在吊销" : "确认吊销"}
              </button>
            </div>
          </>
        )}
      </ProviderDialog>
    </main>
  );
}
