import type { LucideIcon } from "lucide-react";

type WorkspaceEmptyStateProps = {
  icon: LucideIcon;
  title: string;
  description: string;
};

export function WorkspaceEmptyState({
  icon: Icon,
  title,
  description,
}: WorkspaceEmptyStateProps) {
  return (
    <section className="workspace-empty-state" aria-labelledby="workspace-empty-title">
      <span className="workspace-empty-icon" aria-hidden="true">
        <Icon />
      </span>
      <h2 id="workspace-empty-title">{title}</h2>
      <p>{description}</p>
    </section>
  );
}
