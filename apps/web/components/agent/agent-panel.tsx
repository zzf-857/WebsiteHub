"use client";

import {
  ArrowUp,
  CircleAlert,
  Globe,
  History,
  Plus,
  Sparkles,
  Square,
} from "lucide-react";
import {
  Fragment,
} from "react";
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
  StaggerList,
} from "@/components/react-bits/stagger-list";
import {
  IDLE_SAVE,
  LibraryResultCard,
  WebResultCard,
  cardKey,
} from "@/components/agent/agent-result-cards";
import { QUICK_PROMPTS, useAgentPanel } from "@/components/agent/use-agent-panel";

export function AgentPanel() {
  const {
    activeToolCalls,
    answerStreaming,
    applyPrompt,
    busy,
    commandPanel,
    conversationId,
    conversationStarted,
    draftStates,
    errorText,
    handleCollect,
    handleConfirmDraft,
    handleInputChange,
    handleInputKeyDown,
    handleSubmit,
    headerTime,
    headerTitle,
    historyGroups,
    historyOpen,
    historyRef,
    historyStatus,
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
  } = useAgentPanel();

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
              仅收藏库
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
              type="button"
              className="agent-head-button"
              aria-haspopup="menu"
              aria-expanded={historyOpen}
              onClick={toggleHistory}
            >
              <History aria-hidden="true" />
              历史
            </button>
            {historyOpen && (
              <div className="agent-history-menu" role="menu" aria-label="历史会话">
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
          <button type="button" className="agent-head-button" onClick={startNewConversation}>
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
              status={status}
              activeToolCalls={activeToolCalls}
              draftStates={draftStates}
              onConfirmDraft={handleConfirmDraft}
              errorText={errorText}
              errorCode={streamError?.code ?? null}
            />

            {busy && stageItems.length > 0 && (
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
            )}

            {resultGroups && (
              <div className="agent-results">
                {resultGroups.library.items.length > 0 && (
                  <section className="agent-result-group" aria-label="来自收藏库的结果">
                    <h3 className="agent-result-label">
                      <span>来自收藏库 · {resultGroups.library.total}</span>
                    </h3>
                    <StaggerList className="agent-result-grid">
                      {resultGroups.library.items.map((link, index) => (
                        <LibraryResultCard key={cardKey(link, index)} link={link} />
                      ))}
                    </StaggerList>
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
                    <StaggerList className="agent-result-grid">
                      {resultGroups.web.items.map((link, index) => (
                        <WebResultCard
                          key={cardKey(link, index)}
                          link={link}
                          providerLabel={resultGroups.web.provider ?? "联网搜索"}
                          state={link.url ? (webSaves[link.url] ?? IDLE_SAVE) : IDLE_SAVE}
                          onCollect={handleCollect}
                        />
                      ))}
                    </StaggerList>
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
                  disabled={!input.trim()}
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
                  disabled={!input.trim()}
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
