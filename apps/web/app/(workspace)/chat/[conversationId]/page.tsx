import { redirect } from "next/navigation";

/**
 * 旧的会话深链继续可用：首页的 Agent 面板支持用 ?c= 恢复指定会话，
 * 所以这里只做一次转发，避免维护第二套对话界面。
 */
export default async function ConversationPage({
  params,
}: Readonly<{ params: Promise<{ conversationId: string }> }>) {
  const { conversationId } = await params;
  redirect(`/?c=${encodeURIComponent(conversationId)}`);
}
