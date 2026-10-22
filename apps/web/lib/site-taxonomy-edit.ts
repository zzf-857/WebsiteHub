// 详情页分类与标签“就地编辑”（Q46）的纯逻辑：选择器过滤、可否新建标签、
// 以及只携带被改字段的更新请求（不会顺手覆盖名称、简介等其他字段）。

import {
  libraryTagNameKey,
  MAX_LIBRARY_TAG_NAME_LENGTH,
  normalizeLibraryTagName,
  type LibraryCategory,
  type LibrarySite,
  type LibrarySiteUpdateInput,
  type LibraryTagRef,
} from "./library-contract.ts";

export const MAX_TAG_PICKER_OPTIONS = 60;

/** 按规范化后的名称包含关系过滤；已选的排在前面，其余保持原顺序。 */
export function filterTagOptions<T extends LibraryTagRef>(
  tags: readonly T[],
  query: string,
  selectedIds: ReadonlySet<string>,
): T[] {
  const key = libraryTagNameKey(query);
  const matches = key ? tags.filter((tag) => libraryTagNameKey(tag.name).includes(key)) : [...tags];
  const selected = matches.filter((tag) => selectedIds.has(tag.id));
  const rest = matches.filter((tag) => !selectedIds.has(tag.id));
  return [...selected, ...rest].slice(0, MAX_TAG_PICKER_OPTIONS);
}

/**
 * 输入框里的文字能否作为“新建标签”提交：规范化后非空、不超长，且不与已有标签
 * 同名（按后端的 casefold 规则比较）。返回规范化后的名称，否则 null。
 */
export function tagCreateCandidate(tags: readonly LibraryTagRef[], query: string): string | null {
  const name = normalizeLibraryTagName(query);
  if (!name || Array.from(name).length > MAX_LIBRARY_TAG_NAME_LENGTH) return null;
  const key = libraryTagNameKey(name);
  return tags.some((tag) => libraryTagNameKey(tag.name) === key) ? null : name;
}

export function toggleTagSelection(ids: readonly string[], id: string): string[] {
  return ids.includes(id) ? ids.filter((current) => current !== id) : [...ids, id];
}

function sameIdSet(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false;
  const set = new Set(left);
  return right.every((id) => set.has(id));
}

/** 改分类的请求；与当前分类相同时返回 null（不发请求）。 */
export function categoryUpdateInput(
  site: Pick<LibrarySite, "version" | "category">,
  categoryId: string,
): LibrarySiteUpdateInput | null {
  if (!categoryId || categoryId === site.category.id) return null;
  return { expectedVersion: site.version, categoryId };
}

/** 改标签的请求（整组替换，保持用户勾选的顺序）；集合没变时返回 null。 */
export function tagsUpdateInput(
  site: Pick<LibrarySite, "version" | "tags">,
  tagIds: readonly string[],
): LibrarySiteUpdateInput | null {
  const unique = Array.from(new Set(tagIds));
  if (sameIdSet(unique, site.tags.map((tag) => tag.id))) return null;
  return { expectedVersion: site.version, tagIds: unique };
}

/** 分类选择器：默认分类排最前，其余按名称。 */
export function sortCategoryOptions(categories: readonly LibraryCategory[]): LibraryCategory[] {
  return [...categories].sort((left, right) => {
    if (left.isDefault !== right.isDefault) return left.isDefault ? -1 : 1;
    return left.name.localeCompare(right.name, "zh-CN");
  });
}
