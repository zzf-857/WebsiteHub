import { redirect } from "next/navigation";

/** 旧的“新对话”深链：/chat 打开时就是一个空白的新会话。 */
export default function NewConversationPage() {
  redirect("/chat");
}
