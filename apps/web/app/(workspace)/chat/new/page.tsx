import { redirect } from "next/navigation";

/**
 * Agent 已内嵌在首页主列，独立的全屏对话页不再存在。
 * 保留这个路由只为让旧书签与外部链接继续可用。
 */
export default function NewConversationPage() {
  redirect("/");
}
