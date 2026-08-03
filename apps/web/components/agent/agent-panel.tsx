"use client";

import {
  ArrowUp,
  ChevronDown,
  CircleAlert,
  Globe,
  History,
  ListChecks,
  Plus,
  Sparkles,
  Square,
} from "lucide-react";
import {
  Fragment,
} from "react";
import {
  AgentResultPagination,
} from "@/components/agent/agent-result-pagination";
import {
  ConversationThread,
} from "@/components/agent/conversation-thread";
import {
  BlurText,
} from "@/components/react-bits/blur-text";
import {
  CountUp,
} from "@/components/react-bits/count-up";
import {
  PopCheck,
} from "@/components/react-bits/pop-check";
import {
  IDLE_SAVE,
  LibraryResultCard,
  WebResultCard,
  cardKey,
} from "@/components/agent/agent-result-cards";
import { QUICK_PROMPTS, useAgentPanel } from "@/components/agent/use-agent-panel";

type AgentPanelProps = {
  onLibraryChanged?: () => void;
};

export function AgentPanel({ onLibraryChanged }: Readonly<AgentPanelProps>) {
  const {
    activeToolCalls,
    activeCommandOptionId,
    answerStreaming,
    applyPrompt,
    busy,
    commandPanel,
    commandPanelId,
    commandPanelOpen,
    conversationId,
    conversationStarted,
    draftStates,
    draftWorkflowBusy,
    errorText,
    handleCollect,
    handleConfirmDraft,
    handleInputChange,
    handleInputKeyDown,
    handleSubmit,
    headerTime,
    headerTitle,
    historyGroups,
    handleHistoryMenuKeyDown,
    handleHistoryTriggerKeyDown,
    historyOpen,
    historyRef,
    historyStatus,
    historyTriggerRef,
    input,
    messages,
    openConversation,
    resultGroups,
    savedCount,
    scope,
    setScope,
    stageItems,
    startNewConversation,
    status,
    stop,
    streamError,
    textareaRef,
    toggleHistory,
    webSaves,
  } = useAgentPanel({ onLibraryChanged });
  const interactionLocked = busy || draftWorkflowBusy;

  return (
    <section
      /* 跨任务契约：顶栏通过观察 #agent-panel 的位置切换吸顶态，id 不能改名 */
      id="agent-panel"
      className="agent-panel"
      data-state={conversationStarted ? "active" : "idle"}
      /* 回答文本流式输出期间置位，CSS 靠它在正文末尾追加闪烁的打字光标 */
      data-streaming={answerStreaming || undefined}
      aria-label="Agent 助手"
    >
      <header className="agent-panel-head">
        <Sparkles className="agent-panel-spark" aria-hidden="true" />
        {conversationStarted ? (
          <>
            <h2 className="agent-panel-title">{headerTitle}</h2>
            {headerTime && <span className="agent-panel-time">{headerTime}</span>}
          </>
        ) : (
          <h2 className="agent-panel-title">
            <BlurText text="今天想找什么网站？" />
          </h2>
        )}
        <div className="agent-head-actions">
          <div className="agent-scope" role="group" aria-label="检索范围">
            <button
              type="button"
              data-active={scope === "collection" || undefined}
              aria-pressed={scope === "collection"}
              onClick={() => setScope("collection")}
            >
              仅网址库
            </button>
            <button
              type="button"
              data-active={scope === "online" || undefined}
              aria-pressed={scope === "online"}
              onClick={() => setScope("online")}
            >
              <Globe aria-hidden="true" />
              允许联网
            </button>
          </div>
          <div className="agent-history" ref={historyRef}>
            <button
              ref={historyTriggerRef}
              type="button"
              className="agent-head-button"
              aria-haspopup="menu"
              aria-expanded={historyOpen}
              disabled={interactionLocked}
              onClick={toggleHistory}
              onKeyDown={handleHistoryTriggerKeyDown}
            >
              <History aria-hidden="true" />
              历史
            </button>
            {historyOpen && (
              <div
                className="agent-history-menu"
                role="menu"
                aria-label="历史会话"
                onKeyDown={handleHistoryMenuKeyDown}
              >
                {historyStatus === "loading" && (
                  <p className="agent-history-note">正在读取历史会话…</p>
                )}
                {historyStatus === "error" && (
                  <p className="agent-history-note" data-tone="danger">
                    历史会话读取失败，请稍后重试。
                  </p>
                )}
                {historyStatus === "ready" && historyGroups.length === 0 && (
                  <p className="agent-history-note">还没有历史会话，完成一次对话后会出现在这里。</p>
                )}
                {historyStatus === "ready" &&
                  historyGroups.map((group) => (
                    <div className="agent-history-group" key={group.key}>
                      <span className="agent-history-label">{group.label}</span>
                      {group.items.map((item) => (
                        <button
                          type="button"
                          role="menuitem"
                          className="agent-history-item"
                          data-active={item.id === conversationId || undefined}
                          disabled={interactionLocked}
                          key={item.id}
                          onClick={() => openConversation(item.id)}
                        >
                          <History aria-hidden="true" />
                          <span className="agent-history-title">{item.title}</span>
                          <span className="agent-history-count">{item.messageCount} 条</span>
                        </button>
                      ))}
                    </div>
                  ))}
              </div>
            )}
          </div>
          <button
            type="button"
            className="agent-head-button"
            disabled={interactionLocked}
            onClick={startNewConversation}
          >
            <Plus aria-hidden="true" />
            新对话
          </button>
        </div>
      </header>

      {conversationStarted ? (
        <>
          <div className="agent-panel-body">
            <ConversationThread
              messages={messages}
              conversationId={conversationId}
              status={status}
              activeToolCalls={activeToolCalls}
              draftStates={draftStates}
              onConfirmDraft={handleConfirmDraft}
              errorText={errorText}
              errorCode={streamError?.code ?? null}
            />

            {stageItems.length > 0 && (
              <details
                className="agent-task-timeline"
                key={busy ? "agent-task-active" : "agent-task-settled"}
                open={busy}
              >
                <summary>
                  <ListChecks aria-hidden="true" />
                  {busy
                    ? "Agent 正在执行"
                    : `已完成 ${stageItems.filter((item) => item.done).length} 步`}
                  <ChevronDown aria-hidden="true" />
                </summary>
                <div className="agent-stages" role="status" aria-live="polite">
                  {stageItems.map((item, index) => (
                    <Fragment key={item.key}>
                      {index > 0 && <span className="agent-stage-gap" aria-hidden="true" />}
                      <span className="agent-stage" data-done={item.done || undefined}>
                        <PopCheck done={item.done} size={14} />
                        {item.label}
                      </span>
                    </Fragment>
                  ))}
                </div>
              </details>
            )}

            {resultGroups && (
              <div className="agent-results">
                {resultGroups.library.items.length > 0 && (
                  <section className="agent-result-group" aria-label="来自网址库的结果">
                    <h3 className="agent-result-label">
                      <span>来自网址库 · {resultGroups.library.total}</span>
                    </h3>
                    <AgentResultPagination
                      key={resultGroups.library.key}
                      items={resultGroups.library.items}
                      ariaLabel="网址库结果分页"
                      renderItem={(link, index) => (
                        <LibraryResultCard key={cardKey(link, index)} link={link} />
                      )}
                    />
                  </section>
                )}
                {resultGroups.web.items.length > 0 && (
                  <section className="agent-result-group" aria-label="来自网络的结果">
                    <h3 className="agent-result-label">
                      <span>
                        来自网络
                        {resultGroups.web.provider
                          ? ` · ${resultGroups.web.provider.toUpperCase()}`
                          : ""}{" "}
                        · {resultGroups.web.items.length}
                      </span>
                      {savedCount > 0 && (
                        <span className="agent-saved-count">
                          已收录 <CountUp value={savedCount} />
                        </span>
                      )}
                    </h3>
                    <AgentResultPagination
                      key={resultGroups.web.key}
                      items={resultGroups.web.items}
                      ariaLabel="联网结果分页"
                      renderItem={(link, index) => (
                        <WebResultCard
                          key={cardKey(link, index)}
                          link={link}
                          providerLabel={resultGroups.web.provider ?? "联网搜索"}
                          state={link.url ? (webSaves[link.url] ?? IDLE_SAVE) : IDLE_SAVE}
                          collectionDisabled={resultGroups.web.collectionDisabled}
                          onCollect={handleCollect}
                        />
                      )}
                    />
                  </section>
                )}
              </div>
            )}
          </div>

          <footer className="agent-panel-foot">
            <form className="agent-followup" onSubmit={handleSubmit}>
              {commandPanel}
              <label className="sr-only" htmlFor="agent-followup-input">
                继续追问
              </label>
              <textarea
                id="agent-followup-input"
                ref={textareaRef}
                rows={1}
                value={input}
                disabled={draftWorkflowBusy}
                role="combobox"
                aria-autocomplete="list"
                aria-expanded={commandPanelOpen}
                aria-controls={commandPanelOpen ? commandPanelId : undefined}
                aria-activedescendant={activeCommandOptionId}
                placeholder="继续追问，或让我把结果加入某个 Space…"
                onChange={handleInputChange}
                onKeyDown={handleInputKeyDown}
              />
              {busy ? (
                <button
                  type="button"
                  className="agent-stop-button"
                  onClick={() => void stop()}
                >
                  <Square aria-hidden="true" />
                  停止生成
                </button>
              ) : (
                <button
                  type="submit"
                  className="agent-send-button"
                  disabled={draftWorkflowBusy || !input.trim()}
                  aria-label="发送"
                  title="发送"
                >
                  <ArrowUp aria-hidden="true" />
                </button>
              )}
            </form>
          </footer>
        </>
      ) : (
        <>
          <form className="agent-composer" onSubmit={handleSubmit}>
            {commandPanel}
            <label className="sr-only" htmlFor="agent-panel-input">
              描述你要找的网站
            </label>
            <textarea
              id="agent-panel-input"
              ref={textareaRef}
              rows={1}
              value={input}
              disabled={draftWorkflowBusy}
              role="combobox"
              aria-autocomplete="list"
              aria-expanded={commandPanelOpen}
              aria-controls={commandPanelOpen ? commandPanelId : undefined}
              aria-activedescendant={activeCommandOptionId}
              placeholder="描述你要找的网站，或粘贴一个/多个 URL 直接入库…"
              onChange={handleInputChange}
              onKeyDown={handleInputKeyDown}
            />
            <div className="agent-composer-bar">
              <span className="agent-command-hint">
                <kbd>/</kbd>
                命令：/搜索 · /存入
              </span>
              <span className="agent-key-hint">Enter 发送 · Shift+Enter 换行</span>
              {busy ? (
                <button
                  type="button"
                  className="agent-send-button"
                  aria-label="停止生成"
                  title="停止生成"
                  onClick={() => void stop()}
                >
                  <Square aria-hidden="true" />
                </button>
              ) : (
                <button
                  type="submit"
                  className="agent-send-button"
                  disabled={draftWorkflowBusy || !input.trim()}
                  aria-label="发送"
                  title="发送"
                >
                  <ArrowUp aria-hidden="true" />
                </button>
              )}
            </div>
          </form>
          <div className="agent-chips" aria-label="快捷提问">
            {QUICK_PROMPTS.map((prompt) => (
              <button
                type="button"
                className="agent-chip"
                key={prompt}
                onClick={() => applyPrompt(prompt)}
              >
                {prompt}
              </button>
            ))}
          </div>
          {errorText && (
            <p className="agent-panel-error" role="alert">
              <CircleAlert aria-hidden="true" />
              {errorText}
            </p>
          )}
        </>
      )}
    </section>
  );
}
