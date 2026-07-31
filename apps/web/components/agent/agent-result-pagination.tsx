"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { type ReactNode, useState } from "react";
import { StaggerList } from "@/components/react-bits/stagger-list";
import {
  agentResultPageSlice,
  agentResultPageTokens,
} from "@/lib/agent-pagination";

type AgentResultPaginationProps<T> = {
  items: readonly T[];
  ariaLabel: string;
  renderItem: (item: T, index: number) => ReactNode;
};

export function AgentResultPagination<T>({
  items,
  ariaLabel,
  renderItem,
}: Readonly<AgentResultPaginationProps<T>>) {
  const [requestedPage, setRequestedPage] = useState(1);
  const page = agentResultPageSlice(items, requestedPage);

  if (page.pageCount === 0) return null;

  return (
    <div className="agent-paginated-results">
      <StaggerList className="agent-result-grid">
        {page.items.map((item, index) => renderItem(item, page.startIndex + index))}
      </StaggerList>
      {page.pageCount > 1 && (
        <nav className="agent-result-pagination" aria-label={ariaLabel}>
          <button
            type="button"
            aria-label="上一页"
            title="上一页"
            disabled={page.page === 1}
            onClick={() => setRequestedPage(Math.max(1, page.page - 1))}
          >
            <ChevronLeft aria-hidden="true" />
          </button>
          {agentResultPageTokens(page.pageCount, page.page).map((token) =>
            typeof token === "number" ? (
              <button
                type="button"
                key={token}
                aria-label={`第 ${token} 页`}
                aria-current={token === page.page ? "page" : undefined}
                data-active={token === page.page || undefined}
                onClick={() => setRequestedPage(token)}
              >
                {token}
              </button>
            ) : (
              <span key={token} aria-hidden="true">…</span>
            ),
          )}
          <button
            type="button"
            aria-label="下一页"
            title="下一页"
            disabled={page.page === page.pageCount}
            onClick={() => setRequestedPage(Math.min(page.pageCount, page.page + 1))}
          >
            <ChevronRight aria-hidden="true" />
          </button>
        </nav>
      )}
    </div>
  );
}
