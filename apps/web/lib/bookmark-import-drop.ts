export const BOOKMARK_DROP_MULTIPLE_FILES_ERROR = "一次只能拖入一个书签文件";
export const BOOKMARK_DROP_EMPTY_ERROR = "没有读取到可上传的书签文件，请重试";
export const BOOKMARK_FILE_TYPE_ERROR =
  "仅支持浏览器导出的 HTML 书签文件（.html 或 .htm）";

type DragItemLike = {
  kind: string;
};

export type DragTransferLike = {
  types?: ArrayLike<string> | null;
  items?: ArrayLike<DragItemLike> | null;
};

export type BookmarkFileLike = {
  name: string;
  type: string;
};

export type BookmarkFileSelection<T extends BookmarkFileLike> =
  | { ok: true; file: T }
  | { ok: false; error: string };

export function isFileDrag(transfer: DragTransferLike | null): boolean {
  if (!transfer) return false;
  if (Array.from(transfer.types ?? []).includes("Files")) return true;
  return Array.from(transfer.items ?? []).some((item) => item.kind === "file");
}

export function bookmarkFileValidationError(file: BookmarkFileLike): string | null {
  const hasHtmlExtension = /\.html?$/i.test(file.name.trim());
  const mimeType = file.type.trim().toLowerCase().split(";", 1)[0];
  return hasHtmlExtension || mimeType === "text/html" ? null : BOOKMARK_FILE_TYPE_ERROR;
}

export function selectDroppedBookmarkFile<T extends BookmarkFileLike>(
  files: ArrayLike<T>,
): BookmarkFileSelection<T> {
  if (files.length === 0) return { ok: false, error: BOOKMARK_DROP_EMPTY_ERROR };
  if (files.length > 1) return { ok: false, error: BOOKMARK_DROP_MULTIPLE_FILES_ERROR };

  const file = files[0];
  if (!file) return { ok: false, error: BOOKMARK_DROP_EMPTY_ERROR };
  const validationError = bookmarkFileValidationError(file);
  return validationError
    ? { ok: false, error: validationError }
    : { ok: true, file };
}
