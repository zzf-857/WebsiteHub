import { Archive, Clock, CodeXml, FolderGit2, GitFork, Scale, Star } from "lucide-react";

import type { LibrarySite } from "@/lib/library-contract";
import { repoPanelModel } from "@/lib/repo-panel";

/* Q45 仓库面板：只读展示分析流程写入 meta.github 的 GitHub 数据
   （星数、fork、语言、topics、许可证、最近推送、是否归档）。
   数据随“重新分析”刷新；这里不发任何请求，也不提供编辑。 */

type RepoPanelCardProps = {
  site: Pick<LibrarySite, "kind" | "meta">;
};

export function RepoPanelCard({ site }: Readonly<RepoPanelCardProps>) {
  if (site.kind !== "repo") return null;
  const model = repoPanelModel(site.meta);
  if (!model) return null;

  return (
    <section className="sd-card sd-side-card" aria-labelledby="sd-repo-title">
      <header className="sd-side-head">
        <FolderGit2 size={16} className="sd-agent-icon" aria-hidden="true" />
        <h2 id="sd-repo-title" className="sd-side-title">
          GitHub 仓库
        </h2>
        <span className="sd-side-spacer" aria-hidden="true" />
        {model.archived && (
          <span className="sd-side-badge sd-repo-archived">
            <Archive size={12} aria-hidden="true" />
            已归档
          </span>
        )}
      </header>

      {model.notice ? (
        <p className="sd-repo-notice">{model.notice}</p>
      ) : (
        <>
          <ul className="sd-repo-facts">
            {model.stars !== null && (
              <li>
                <Star size={14} aria-hidden="true" />
                <strong>{model.stars}</strong>
                <span className="sd-repo-label">星标</span>
              </li>
            )}
            {model.forks !== null && (
              <li>
                <GitFork size={14} aria-hidden="true" />
                <strong>{model.forks}</strong>
                <span className="sd-repo-label">Fork</span>
              </li>
            )}
            {model.language && (
              <li>
                <CodeXml size={14} aria-hidden="true" />
                {model.language}
              </li>
            )}
            {model.license && (
              <li>
                <Scale size={14} aria-hidden="true" />
                {model.license}
              </li>
            )}
            {model.pushed && (
              <li>
                <Clock size={14} aria-hidden="true" />
                {model.pushed}
              </li>
            )}
          </ul>
          {model.topics.length > 0 && (
            <ul className="sd-chip-row sd-repo-topics" aria-label="仓库主题">
              {model.topics.map((topic) => (
                <li key={topic} className="sd-chip sd-chip-outline">
                  {topic}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
