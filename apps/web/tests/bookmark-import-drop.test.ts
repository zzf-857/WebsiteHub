import test from "node:test";
import assert from "node:assert/strict";

import {
  BOOKMARK_DROP_EMPTY_ERROR,
  BOOKMARK_DROP_MULTIPLE_FILES_ERROR,
  BOOKMARK_FILE_TYPE_ERROR,
  bookmarkFileValidationError,
  isFileDrag,
  selectDroppedBookmarkFile,
} from "../lib/bookmark-import-drop.ts";

function file(name: string, type = "") {
  return { name, type };
}

test("文件拖拽可通过 Files 类型或 file item 识别", () => {
  assert.equal(isFileDrag({ types: ["Files"] }), true);
  assert.equal(isFileDrag({ types: [], items: [{ kind: "file" }] }), true);
});

test("文本和链接拖拽不会被书签上传接管", () => {
  assert.equal(isFileDrag({ types: ["text/plain", "text/uri-list"] }), false);
  assert.equal(isFileDrag({ types: [], items: [{ kind: "string" }] }), false);
  assert.equal(isFileDrag(null), false);
});

test("html、htm 与 text/html MIME 都可作为书签文件", () => {
  assert.equal(bookmarkFileValidationError(file("bookmarks.html")), null);
  assert.equal(bookmarkFileValidationError(file("BOOKMARKS.HTM")), null);
  assert.equal(bookmarkFileValidationError(file("browser-export", "text/html")), null);
  assert.equal(bookmarkFileValidationError(file("browser-export", "text/html; charset=utf-8")), null);
});

test("Windows 未提供 MIME 时仍按正确扩展名接收", () => {
  const selection = selectDroppedBookmarkFile([file("edge-bookmarks.html", "")]);
  assert.equal(selection.ok, true);
});

test("非 HTML 文件在前端直接拒绝", () => {
  assert.equal(bookmarkFileValidationError(file("bookmarks.json", "application/json")), BOOKMARK_FILE_TYPE_ERROR);
  assert.deepEqual(selectDroppedBookmarkFile([file("notes.txt", "text/plain")]), {
    ok: false,
    error: BOOKMARK_FILE_TYPE_ERROR,
  });
});

test("空投放和多文件投放返回明确错误", () => {
  assert.deepEqual(selectDroppedBookmarkFile([]), {
    ok: false,
    error: BOOKMARK_DROP_EMPTY_ERROR,
  });
  assert.deepEqual(
    selectDroppedBookmarkFile([file("one.html"), file("two.html")]),
    { ok: false, error: BOOKMARK_DROP_MULTIPLE_FILES_ERROR },
  );
});
