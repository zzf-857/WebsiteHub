import { Sparkles } from "lucide-react";
import Link from "next/link";

/* 取舍说明（设计稿 1d「让 Agent 帮你」）：Agent 对话入口在首页；
   目前首页不支持通过 URL 携带预填提问，这几条快捷入口只负责把用户
   带到 Agent，文案本身帮助用户复述意图，不假装能直接触发任务。 */

type AgentHelpCardProps = {
  siteName: string;
};

/** 站点名过长时截断，避免建议文案在窄卡片里溢出换行过多 */
function clipName(name: string, max = 12): string {
  const chars = Array.from(name.trim());
  return chars.length > max ? `${chars.slice(0, max).join("")}…` : name.trim();
}

export function AgentHelpCard({ siteName }: Readonly<AgentHelpCardProps>) {
  const short = clipName(siteName);
  const suggestions = [
    `找几个和「${short}」类似的网站`,
    `把「${short}」加入合适的 Space`,
    "更新这条收录的描述和标签",
  ];

  return (
    <section className="sd-card sd-side-card" aria-labelledby="sd-agent-title">
      <header className="sd-side-head">
        <Sparkles size={16} className="sd-agent-icon" aria-hidden="true" />
        <h2 id="sd-agent-title" className="sd-side-title">
          让 Agent 帮你
        </h2>
      </header>
      <div className="sd-suggest-list">
        {suggestions.map((text) => (
          <Link key={text} className="sd-suggest" href="/">
            {text}
          </Link>
        ))}
      </div>
    </section>
  );
}
