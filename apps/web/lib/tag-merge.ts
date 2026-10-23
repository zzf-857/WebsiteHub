// 标签合并（Q52，FR-OR-003）的界面文案与选项。纯函数，Node 测试直接覆盖。

import type { LibraryTag, LibraryTagMergePreview } from "./library-contract.ts";

export type TagMergeTargetOption = { value: string; label: string; keywords: readonly string[] };

/** 可选的合并目标：除源标签以外的全部标签，沿用标签列表的顺序（后端已按名称排序），附上收藏数。 */
export function tagMergeTargetOptions(tags: readonly LibraryTag[], sourceId: string): TagMergeTargetOption[] {
  return tags
    .filter((tag) => tag.id !== sourceId)
    .map((tag) => ({ value: tag.id, label: `${tag.name}（${tag.siteCount}）`, keywords: [tag.name] }));
}

/** 二次确认前的影响说明。 */
export function tagMergeSummary(preview: LibraryTagMergePreview): string {
  const source = `「${preview.source.name}」`;
  const target = `「${preview.target.name}」`;
  if (preview.affectedSiteCount === 0) {
    return `${source}没有关联收藏，合并后直接删除，${target}不变。`;
  }
  const moved = preview.affectedSiteCount - preview.alreadyTaggedCount;
  const overlap = preview.alreadyTaggedCount > 0
    ? `，其中 ${preview.alreadyTaggedCount} 个原本就带${target}`
    : "";
  return `${preview.affectedSiteCount} 个收藏会改带${target}${overlap}（新增关联 ${moved} 个）；合并后删除${source}，不能撤销。`;
}
