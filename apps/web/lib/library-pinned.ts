// 收藏库“置顶收藏”分区的呈现方式（纯函数，Node 测试覆盖）：
// 置顶很少时整张大卡太占地方，改成一排紧凑的横向条目；达到阈值后保持原来的网格。

/** 少于这个数量的置顶收藏用紧凑条目。 */
export const PINNED_CHIP_LIMIT = 4;

/** 置顶已全部加载（没有下一页）且数量少于阈值时用紧凑条目。 */
export function shouldUsePinnedChips(count: number, hasMore: boolean): boolean {
  return !hasMore && count > 0 && count < PINNED_CHIP_LIMIT;
}
