import type { Metadata } from "next";

import { ChatPage } from "@/components/agent/chat-page";

export const metadata: Metadata = { title: "WebHub · 站内对话" };

/**
 * 站内对话（2026-10 D2：冻结，保持可用、只修缺陷）。
 * AgentPanel 用 window.location 的 ?c= / ?ask= 恢复会话与预填提问，与路由无关，
 * 所以这里直接全宽渲染同一个面板。
 */
export default function ConversationListPage() {
  return <ChatPage />;
}
