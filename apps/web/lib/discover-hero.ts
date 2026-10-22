// 发现页“今日重温”主卡队列（Q44）。纯 reducer：组件负责调接口与动效，
// 这里只回答“现在主卡是哪一条、撤销后放回哪里”。
//
// - 处理（打开 / 已看过 / 下周再提醒 / 不再推荐）让该项离开队列，原位置的下一项补位；
// - 撤销把它放回原来的位置并重新提为主卡；
// - 接口失败时同样用 restore 回滚，所以离开的项都按 id 记在 removed 里，
//   而不只是最后一条（较早的请求可能晚于较新的操作才失败）。

import type {
  LibraryRediscoverItem,
  LibrarySite,
  LibrarySiteStateInput,
} from "./library-contract.ts";

export type HeroEntry = LibraryRediscoverItem;
export type HeroDismissal = "open" | "seen" | "snooze" | "mute";

type RemovedEntry = Readonly<{ entry: HeroEntry; dismissal: HeroDismissal }>;

export type HeroState = Readonly<{
  entries: readonly HeroEntry[];
  activeId: string | null;
  removed: readonly RemovedEntry[];
  /** 本批次的原始顺序：撤销按它放回原位，多次处理后乱序撤销也不会打乱队列。 */
  order: readonly string[];
}>;

export type HeroAction =
  | { type: "load"; entries: readonly HeroEntry[] }
  | { type: "select"; id: string }
  | { type: "dismiss"; id: string; dismissal: HeroDismissal }
  | { type: "restore"; id: string }
  | { type: "update"; site: LibrarySite };

/** 只保留最近若干条可撤销记录，防止长时间停留时无限增长。 */
const MAX_REMOVED = 12;

export const SNOOZE_DAYS = 7;

export function initialHeroState(entries: readonly HeroEntry[] = []): HeroState {
  return {
    entries: [...entries],
    activeId: entries[0]?.site.id ?? null,
    removed: [],
    order: entries.map((entry) => entry.site.id),
  };
}

export function heroReducer(state: HeroState, action: HeroAction): HeroState {
  switch (action.type) {
    case "load":
      return initialHeroState(action.entries);

    case "select":
      if (!state.entries.some((entry) => entry.site.id === action.id)) return state;
      return state.activeId === action.id ? state : { ...state, activeId: action.id };

    case "dismiss": {
      const index = state.entries.findIndex((entry) => entry.site.id === action.id);
      if (index < 0) return state;
      const entry = state.entries[index];
      const entries = state.entries.filter((_, position) => position !== index);
      // 被处理的若是主卡，原位置的下一项补位；处理最后一项时回到队首
      const activeId = state.activeId === action.id
        ? (entries[index] ?? entries[0])?.site.id ?? null
        : state.activeId;
      const removed = [
        ...state.removed.filter((item) => item.entry.site.id !== action.id),
        { entry, dismissal: action.dismissal },
      ].slice(-MAX_REMOVED);
      return { ...state, entries, activeId, removed };
    }

    case "restore": {
      const record = state.removed.find((item) => item.entry.site.id === action.id);
      if (!record || state.entries.some((entry) => entry.site.id === action.id)) return state;
      const rank = state.order.indexOf(action.id);
      const before = state.entries.findIndex((entry) => state.order.indexOf(entry.site.id) > rank);
      const entries = [...state.entries];
      entries.splice(before < 0 ? entries.length : before, 0, record.entry);
      return {
        ...state,
        entries,
        activeId: record.entry.site.id,
        removed: state.removed.filter((item) => item !== record),
      };
    }

    case "update": {
      const replace = (entry: HeroEntry): HeroEntry =>
        entry.site.id === action.site.id ? { ...entry, site: action.site } : entry;
      return {
        ...state,
        entries: state.entries.map(replace),
        removed: state.removed.map((item) => ({ ...item, entry: replace(item.entry) })),
      };
    }
  }
}

export function heroActive(state: HeroState): HeroEntry | null {
  return state.entries.find((entry) => entry.site.id === state.activeId) ?? null;
}

/** 眉行的 “1 / 4”：index 从 0 开始，total 为剩余条数。 */
export function heroPosition(state: HeroState): { index: number; total: number } {
  const index = state.entries.findIndex((entry) => entry.site.id === state.activeId);
  return { index: Math.max(0, index), total: state.entries.length };
}

/** “接下来”：主卡之后的项，绕回队首，最多 limit 条，不含主卡本身。 */
export function heroUpNext(state: HeroState, limit = 3): HeroEntry[] {
  const { index, total } = heroPosition(state);
  const result: HeroEntry[] = [];
  for (let step = 1; step < total && result.length < limit; step += 1) {
    result.push(state.entries[(index + step) % total]);
  }
  return result;
}

/** 各处理动作对应的状态更新；“打开”走单独的打开接口，返回 null。 */
export function heroDismissInput(dismissal: HeroDismissal): LibrarySiteStateInput | null {
  if (dismissal === "seen") return { seen: true };
  if (dismissal === "snooze") return { snoozeDays: SNOOZE_DAYS };
  if (dismissal === "mute") return { resurfaceMuted: true };
  return null;
}

/** 撤销用的反向更新；打开无法撤销（打开记录是事实），返回 null。 */
export function heroUndoInput(dismissal: HeroDismissal): LibrarySiteStateInput | null {
  if (dismissal === "seen") return { seen: false };
  if (dismissal === "snooze") return { snoozeDays: 0 };
  if (dismissal === "mute") return { resurfaceMuted: false };
  return null;
}

export function heroToastMessage(dismissal: HeroDismissal): string | null {
  if (dismissal === "seen") return "已标记为已看";
  if (dismissal === "snooze") return "下周再提醒你";
  if (dismissal === "mute") return "不再推荐";
  return null;
}
