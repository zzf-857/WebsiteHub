import { Archive, Clock, GitFork, Scale, Star } from "lucide-react";
import type { CSSProperties } from "react";

import type { LibrarySite } from "@/lib/library-contract";
import { repoPanelModel } from "@/lib/repo-panel";
import { repoCoordinates, siteHue } from "@/lib/resource-cover";

// 详情页仓库面板（Q46）：只展示 Q45 写进 site.meta.github 的数据；没有就整块不渲染。

type RepoPanelProps = {
  site: Pick<LibrarySite, "meta" | "originalUrl">;
  now: number;
};

export function RepoPanel({ site, now }: Readonly<RepoPanelProps>) {
  const model = repoPanelModel(site.meta, now);
  if (!model) return null;
  const coordinates = repoCoordinates(site);

  return (
    <section className="sd-card sd-section sd-repo" aria-labelledby="sd-repo-title">
      <header className="sd-repo-head">
        <h2 id="sd-repo-title" className="sd-section-title">
          仓库
          {coordinates && (
            <span className="sd-repo-slug">{coordinates.owner}/{coordinates.repo}</span>
          )}
        </h2>
        {model.archived && (
          <span className="sd-repo-archived">
            <Archive aria-hidden="true" />
            已归档
          </span>
        )}
      </header>
      <dl className="sd-repo-stats">
        {model.stars !== null && (
          <div>
            <dt><Star aria-hidden="true" />星标</dt>
            <dd>{model.stars}</dd>
          </div>
        )}
        {model.forks !== null && (
          <div>
            <dt><GitFork aria-hidden="true" />Fork</dt>
            <dd>{model.forks}</dd>
          </div>
        )}
        {model.language && (
          <div>
            <dt>
              <span
                className="sd-repo-language-dot"
                style={{ "--site-hue": siteHue(model.language) } as CSSProperties}
                aria-hidden="true"
              />
              语言
            </dt>
            <dd>{model.language}</dd>
          </div>
        )}
        {model.license && (
          <div>
            <dt><Scale aria-hidden="true" />许可证</dt>
            <dd>{model.license}</dd>
          </div>
        )}
        {model.pushed && (
          <div>
            <dt><Clock aria-hidden="true" />最近推送</dt>
            <dd>
              {model.pushedAt ? <time dateTime={model.pushedAt}>{model.pushed}</time> : model.pushed}
            </dd>
          </div>
        )}
      </dl>
      {model.topics.length > 0 && (
        <ul className="sd-chip-row sd-repo-topics" aria-label="主题">
          {model.topics.map((topic) => (
            <li key={topic} className="sd-chip sd-chip-outline">{topic}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
