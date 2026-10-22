// 发现页瀑布流的分列算法（Q44）。纯函数：只认 id 与估算高度，不碰 DOM。
//
// 规则：逐项放进“当前最短”的列，等高时取最左列。贪心放置的结果只取决于
// 前面已放置的项，所以追加新一页时，旧卡片所在的列与顺序保持不变——
// 无限加载不会让已经看过的卡片跳位。

export type MasonryItem = Readonly<{ id: string; height: number }>;

export type MasonryLayout = Readonly<{
  columns: readonly (readonly string[])[];
  heights: readonly number[];
}>;

/** 容器宽度 → 列数：≥1280 四列、≥960 三列、≥600 两列，否则单列。 */
export function masonryColumnCount(width: number): number {
  if (!Number.isFinite(width) || width < 600) return 1;
  if (width >= 1280) return 4;
  if (width >= 960) return 3;
  return 2;
}

export function createMasonryLayout(columnCount: number): MasonryLayout {
  const count = Math.max(1, Math.trunc(columnCount) || 1);
  return {
    columns: Array.from({ length: count }, () => []),
    heights: Array.from({ length: count }, () => 0),
  };
}

function shortestColumn(heights: readonly number[]): number {
  let index = 0;
  for (let candidate = 1; candidate < heights.length; candidate += 1) {
    if (heights[candidate] < heights[index]) index = candidate;
  }
  return index;
}

/**
 * 把新项追加进已有布局，返回新布局（不修改入参）。
 * `gap` 是同列卡片的纵向间距：第二张起才计入，避免首卡多算一截。
 * 已经放置过的 id 会被跳过，重复的分页响应不会让同一张卡出现两次。
 */
export function appendMasonryItems(
  layout: MasonryLayout,
  items: readonly MasonryItem[],
  gap = 0,
): MasonryLayout {
  const columns = layout.columns.map((column) => [...column]);
  const heights = [...layout.heights];
  const placed = new Set(columns.flat());
  for (const item of items) {
    if (placed.has(item.id)) continue;
    placed.add(item.id);
    const index = shortestColumn(heights);
    const height = Number.isFinite(item.height) ? Math.max(0, item.height) : 0;
    heights[index] += (columns[index].length > 0 ? gap : 0) + height;
    columns[index].push(item.id);
  }
  return { columns, heights };
}

/** 从零排一遍（列数变化时使用）。 */
export function layoutMasonry(
  items: readonly MasonryItem[],
  columnCount: number,
  gap = 0,
): MasonryLayout {
  return appendMasonryItems(createMasonryLayout(columnCount), items, gap);
}
