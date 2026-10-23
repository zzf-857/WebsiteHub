// 发现页“今日重温”主卡队列（Q44）。纯 reducer：组件负责调接口与动效，
// 这里只回答“现在主卡是哪一条、撤销后放回哪里”。
//
// - 处理（打开 / 已看过 / 下周再提醒 / 不再推荐）让该项离开队列，原位置的下一项补位；
// - 撤销把它放回原来的位置并重新提为主卡；
// - 接口失败时同样用 restore 回滚，所以离开的项都按 id 记在 removed 里，
//   而不只是最后一条（较早的请求可能晚于较新的操作才失败）；
// - 画廊等别处的操作经 update 同步：不再符合重温条件的项移出，回滚 / 撤销后重新符合的放回（Q52）；
// - 撤销记录跨“换一批”保留，换批之后仍能撤销。

import type {
  LibraryRediscoverItem,
  LibrarySite,
  LibrarySiteStateInput,
} from "./library-contract.ts";
import { parseServerTime } from "./relative-time.ts";
import { isUnopened } from "./site-usage.ts";

export type HeroEntry = LibraryRediscoverItem;
export type HeroDismissal = "open" | "seen" | "snooze" | "mute";

type RemovedEntry = Readonly<{ entry: HeroEntry; dismissal: HeroDismissal }>;

export type HeroState = Readonly<{
  entries: readonly HeroEntry[];
  activeId: string | null;
  removed: readonly RemovedEntry[];
  /** 本批次的原始顺序：撤销按它放回原位，多次处理后乱序撤销也不会打乱队列。 */
  order: readonly string[];
  /**
   * 每条入队时的 resurfaceDueAt：队列里本就有“推迟已到期”的项，
   * 只有换成新的推迟时间才算被推迟走（不拿浏览器时钟和服务端比较）。
   */
  queuedDueAt: Readonly<Record<string, string | null>>;
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
    queuedDueAt: Object.fromEntries(entries.map((entry) => [entry.site.id, entry.site.resurfaceDueAt])),
  };
}

/**
 * 这条收藏是否还该留在今日重温里：从未打开、没标记已看、没静音，
 * 也没有被推迟到新的时间（入队时就带着的到期推迟不算）。
 */
export function heroStillQueued(site: LibrarySite, queuedDueAt: string | null): boolean {
  return isUnopened(site)
    && !site.resurfaceMuted
    && (site.resurfaceDueAt === null || sameInstant(site.resurfaceDueAt, queuedDueAt));
}

/** 不同接口可能给出带 / 不带时区后缀的同一时刻：按时间值比较。 */
function sameInstant(value: string, other: string | null): boolean {
  if (other === null) return false;
  if (value === other) return true;
  const left = parseServerTime(value);
  return !Number.isNaN(left) && left === parseServerTime(other);
}

/** 别处（画廊、快捷键）的操作让它离开队列时，按新状态推断是哪种处理。 */
function inferredDismissal(site: LibrarySite): HeroDismissal {
  if (site.openCount > 0) return "open";
  if (site.seenAt !== null) return "seen";
  if (site.resurfaceMuted) return "mute";
  return "snooze";
}

function removeAt(state: HeroState, index: number, dismissal: HeroDismissal): HeroState {
  const entry = state.entries[index];
  const id = entry.site.id;
  const entries = state.entries.filter((_, position) => position !== index);
  // 被处理的若是主卡，原位置的下一项补位；处理最后一项时回到队首
  const activeId = state.activeId === id
    ? (entries[index] ?? entries[0])?.site.id ?? null
    : state.activeId;
  const removed = [
    ...state.removed.filter((item) => item.entry.site.id !== id),
    { entry, dismissal },
  ].slice(-MAX_REMOVED);
  return { ...state, entries, activeId, removed };
}

/**
 * 把撤销记录放回队列：按本批次原顺序找位置；来自上一批（“换一批”之后撤销）的项不在
 * order 里，放到队首。activate 为 false 时（别处回滚）不抢主卡，除非队列原本是空的。
 */
function reinsert(state: HeroState, record: RemovedEntry, entry: HeroEntry, activate: boolean): HeroState {
  const id = entry.site.id;
  const rank = state.order.indexOf(id);
  const before = state.entries.findIndex((item) => state.order.indexOf(item.site.id) > rank);
  const entries = [...state.entries];
  entries.splice(before < 0 ? entries.length : before, 0, entry);
  return {
    ...state,
    entries,
    activeId: activate || state.activeId === null ? id : state.activeId,
    removed: state.removed.filter((item) => item !== record),
  };
}

export function heroReducer(state: HeroState, action: HeroAction): HeroState {
  switch (action.type) {
    case "load": {
      // 撤销记录跨批次保留：“换一批”之后点撤销仍能把该项放回来。
      // 还没落地的处理可能让新一批又带回同一条，按本地记录剔除。
      const removedIds = new Set(state.removed.map((item) => item.entry.site.id));
      const entries = action.entries.filter((entry) => !removedIds.has(entry.site.id));
      const next = initialHeroState(entries);
      const carried = Object.fromEntries(
        state.removed.map((item) => [item.entry.site.id, state.queuedDueAt[item.entry.site.id] ?? null]),
      );
      return { ...next, removed: state.removed, queuedDueAt: { ...carried, ...next.queuedDueAt } };
    }

    case "select":
      if (!state.entries.some((entry) => entry.site.id === action.id)) return state;
      return state.activeId === action.id ? state : { ...state, activeId: action.id };

    case "dismiss": {
      const index = state.entries.findIndex((entry) => entry.site.id === action.id);
      if (index < 0) return state;
      return removeAt(state, index, action.dismissal);
    }

    case "restore": {
      const record = state.removed.find((item) => item.entry.site.id === action.id);
      if (!record || state.entries.some((entry) => entry.site.id === action.id)) return state;
      return reinsert(state, record, record.entry, true);
    }

    case "update": {
      const { site } = action;
      const queuedDueAt = state.queuedDueAt[site.id] ?? null;
      const index = state.entries.findIndex((entry) => entry.site.id === site.id);
      if (index >= 0) {
        const current = state.entries[index];
        const entry = { ...current, site };
        const entries = state.entries.map((item, position) => (position === index ? entry : item));
        const updated = { ...state, entries };
        // 只在“还在队列 → 不该在队列”的那一下移出：撤销刚放回、旧请求才落地的
        // 中间状态不会把它再次挤掉
        const leaves = heroStillQueued(current.site, queuedDueAt) && !heroStillQueued(site, queuedDueAt);
        return leaves ? removeAt(updated, index, inferredDismissal(site)) : updated;
      }
      const record = state.removed.find((item) => item.entry.site.id === site.id);
      if (!record) return state;
      const entry = { ...record.entry, site };
      // 别处撤销或失败回滚让它重新符合条件：悄悄放回队列，不抢主卡
      if (!heroStillQueued(record.entry.site, queuedDueAt) && heroStillQueued(site, queuedDueAt)) {
        return reinsert(state, record, entry, false);
      }
      return {
        ...state,
        removed: state.removed.map((item) => (item === record ? { ...item, entry } : item)),
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

/** 后端理由的分句符：第一句固定是“N 天前收藏，一次都没打开过”。 */
const REASON_SEPARATOR = "；";

/**
 * 推荐理由里超出“收藏时长”的那部分（如“和你最近常收藏的「Agent」相关”“推迟过 2 次，提醒已到期”）。
 * 第一句与眉行的“N 天前收藏 · 从未打开”重复，不再展示；没有额外分句时返回 null。
 */
export function heroReasonDetail(reason: string): string | null {
  const index = reason.indexOf(REASON_SEPARATOR);
  if (index < 0) return null;
  const detail = reason
    .slice(index + REASON_SEPARATOR.length)
    .split(REASON_SEPARATOR)
    .map((part) => part.trim())
    .filter(Boolean)
    .join(REASON_SEPARATOR);
  return detail || null;
}
