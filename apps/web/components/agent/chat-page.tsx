"use client";

import { AgentPanel } from "@/components/agent/agent-panel";

// /chat：站内 Agent 对话的独立页（2026-10 D2 冻结，入口在账号菜单）。
// 面板本身不变，只是从首页主列挪到这里全宽展示。
export function ChatPage() {
  return (
    <main className="chat-page">
      <h1 className="sr-only">站内对话</h1>
      <AgentPanel />
    </main>
  );
}
