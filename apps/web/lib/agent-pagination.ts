export const AGENT_RESULT_PAGE_SIZE = 12;

export type AgentPageToken = number | "ellipsis-start" | "ellipsis-end";

export function agentResultPageCount(itemCount: number, pageSize = AGENT_RESULT_PAGE_SIZE): number {
  if (!Number.isSafeInteger(itemCount) || itemCount <= 0) return 0;
  if (!Number.isSafeInteger(pageSize) || pageSize <= 0) return 0;
  return Math.ceil(itemCount / pageSize);
}

export function agentResultPageSlice<T>(
  items: readonly T[],
  page: number,
  pageSize = AGENT_RESULT_PAGE_SIZE,
): { items: readonly T[]; page: number; startIndex: number; pageCount: number } {
  const pageCount = agentResultPageCount(items.length, pageSize);
  if (pageCount === 0) return { items: [], page: 0, startIndex: 0, pageCount: 0 };
  const safePage = Number.isSafeInteger(page) ? Math.min(Math.max(page, 1), pageCount) : 1;
  const startIndex = (safePage - 1) * pageSize;
  return {
    items: items.slice(startIndex, startIndex + pageSize),
    page: safePage,
    startIndex,
    pageCount,
  };
}

export function agentResultPageTokens(pageCount: number, currentPage: number): AgentPageToken[] {
  if (!Number.isSafeInteger(pageCount) || pageCount <= 0) return [];
  const page = Math.min(Math.max(Number.isSafeInteger(currentPage) ? currentPage : 1, 1), pageCount);
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, index) => index + 1);

  const pages = new Set([1, pageCount, page - 1, page, page + 1]);
  if (page <= 3) {
    pages.add(2);
    pages.add(3);
    pages.add(4);
  }
  if (page >= pageCount - 2) {
    pages.add(pageCount - 1);
    pages.add(pageCount - 2);
    pages.add(pageCount - 3);
  }
  const ordered = [...pages]
    .filter((candidate) => candidate >= 1 && candidate <= pageCount)
    .sort((left, right) => left - right);
  const tokens: AgentPageToken[] = [];
  for (const candidate of ordered) {
    const previous = tokens[tokens.length - 1];
    if (typeof previous === "number" && candidate - previous > 1) {
      tokens.push(previous === 1 ? "ellipsis-start" : "ellipsis-end");
    }
    tokens.push(candidate);
  }
  return tokens;
}
