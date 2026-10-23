import { LibraryApiError } from "./library-client.ts";
import { libraryErrorDetails } from "./library-contract.ts";

// 书签来源台账（Q55）：网站来自哪些浏览器书签目录。只读，账号作用域由后端保证。

export type SiteImportOrigins = {
  occurrenceCount: number;
  /** 每条是从书签文件根开始的目录链；空数组表示书签文件根目录。 */
  folderPaths: string[][];
  truncated: boolean;
};

const EMPTY: SiteImportOrigins = { occurrenceCount: 0, folderPaths: [], truncated: false };

export function normalizeSiteImportOrigins(payload: unknown): SiteImportOrigins {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return EMPTY;
  const record = payload as Record<string, unknown>;
  const rawPaths = Array.isArray(record.folder_paths) ? record.folder_paths : [];
  const folderPaths = rawPaths
    .filter((path): path is unknown[] => Array.isArray(path))
    .map((path) => path.filter((name): name is string => typeof name === "string"));
  const count = record.occurrence_count;
  return {
    occurrenceCount:
      typeof count === "number" && Number.isFinite(count) && count > 0 ? Math.trunc(count) : 0,
    folderPaths,
    truncated: record.truncated === true,
  };
}

export const ROOT_FOLDER_LABEL = "书签根目录";

export function formatFolderPath(path: readonly string[]): string {
  const names = path.map((name) => name.trim()).filter(Boolean);
  return names.length ? names.join(" / ") : ROOT_FOLDER_LABEL;
}

/**
 * 紧凑的一行：最多列出 `maxShown` 个目录，其余折成“等 N 处”。
 * 没有任何来源时返回 null（详情页就不显示这一行）。
 */
export function importOriginsSummary(
  origins: SiteImportOrigins,
  maxShown = 2,
): { text: string; full: string } | null {
  if (!origins.folderPaths.length) return null;
  const labels = [...new Set(origins.folderPaths.map(formatFolderPath))];
  const shown = labels.slice(0, Math.max(1, maxShown));
  const hidden = labels.length - shown.length;
  const more = hidden > 0 || origins.truncated
    ? ` 等 ${labels.length}${origins.truncated ? "+" : ""} 个目录`
    : "";
  return {
    text: `来自书签：${shown.join("、")}${more}`,
    full: labels.join("\n"),
  };
}

export async function getSiteImportOrigins(
  siteId: string,
  signal?: AbortSignal,
): Promise<SiteImportOrigins> {
  const id = siteId.trim();
  if (!id) throw new TypeError("资源 ID 不能为空");
  const response = await fetch(
    `/api/backend/library/sites/${encodeURIComponent(id)}/import-origins`,
    { cache: "no-store", credentials: "include", signal },
  );
  let payload: unknown = null;
  if ((response.headers.get("content-type") ?? "").includes("application/json")) {
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    const error = libraryErrorDetails(response.status, payload);
    throw new LibraryApiError(response.status, error.message, error.code);
  }
  return normalizeSiteImportOrigins(payload);
}
