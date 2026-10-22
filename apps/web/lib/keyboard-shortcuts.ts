// 全站键盘快捷键（Q46）的纯逻辑：按键 → 命令的分发、两键序列（g d / g l / g s）、
// 输入场景判断与焦点移动。DOM 与路由交给 components/keyboard，这里只做可测的决策。

export type ShortcutCommand =
  | "search"
  | "go-discover"
  | "go-library"
  | "go-spaces"
  | "focus-next"
  | "focus-prev"
  | "open"
  | "star"
  | "seen"
  | "snooze"
  | "help";

/** 作用在“当前收藏”上的命令：画廊 / 列表里聚焦的那一项，或详情页本身。 */
export type ShortcutItemCommand = Extract<ShortcutCommand, "open" | "star" | "seen" | "snooze">;

export const SHORTCUT_ITEM_COMMANDS: ReadonlySet<ShortcutCommand> = new Set<ShortcutCommand>([
  "open",
  "star",
  "seen",
  "snooze",
]);

export function isShortcutItemCommand(command: ShortcutCommand): command is ShortcutItemCommand {
  return SHORTCUT_ITEM_COMMANDS.has(command);
}

/** 两键序列的等待时间：按下 g 之后这么久内没有第二键，就当作没按过。 */
export const SHORTCUT_SEQUENCE_TIMEOUT_MS = 1_200;

export type ShortcutKeyEvent = {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  repeat?: boolean;
  /** 输入法组字中（中文输入时按字母键）一律不是快捷键。 */
  isComposing?: boolean;
};

export type ShortcutContext = {
  now: number;
  /** 焦点在文本输入、下拉框、可编辑区域等自己处理按键的控件里。 */
  typing: boolean;
  /** 有模态弹层打开（含快捷键帮助本身）：页面级快捷键全部让路。 */
  blocked: boolean;
  /** 焦点正停在画廊 / 列表的某一项上：只有这时方向键才移动焦点，否则保留浏览器滚动。 */
  itemFocused: boolean;
};

export type ShortcutState = {
  /** 已按下、等待第二键的前缀（目前只有 g）。 */
  pending: "g" | null;
  pendingAt: number;
};

export const INITIAL_SHORTCUT_STATE: ShortcutState = { pending: null, pendingAt: 0 };

export type ShortcutResolution = {
  state: ShortcutState;
  command: ShortcutCommand | null;
  /** 命中快捷键（含序列前缀）时阻止浏览器默认行为，例如 “/” 不再写进刚聚焦的搜索框。 */
  preventDefault: boolean;
};

const GO_TARGETS: Readonly<Record<string, ShortcutCommand>> = {
  d: "go-discover",
  l: "go-library",
  s: "go-spaces",
};

const SINGLE_KEYS: Readonly<Record<string, ShortcutCommand>> = {
  j: "focus-next",
  k: "focus-prev",
  o: "open",
  s: "star",
  e: "seen",
  z: "snooze",
};

const ARROW_KEYS: Readonly<Record<string, ShortcutCommand>> = {
  ArrowDown: "focus-next",
  ArrowRight: "focus-next",
  ArrowUp: "focus-prev",
  ArrowLeft: "focus-prev",
};

function idle(command: ShortcutCommand | null = null, preventDefault = command !== null): ShortcutResolution {
  return { state: INITIAL_SHORTCUT_STATE, command, preventDefault };
}

/**
 * 决定一次按键对应的命令。规则：
 * - ⌘K / Ctrl+K 打开搜索；输入场景与弹层打开时不抢（与原顶栏行为一致）。
 * - 单键快捷键只在没有 ⌘ / Ctrl / Alt 修饰时生效；字母键还要求没按 Shift，
 *   “?” 与 “/” 在不同键盘布局上可能带 Shift，所以不看 Shift。
 * - 输入场景、输入法组字、模态弹层打开时一律不响应单键。
 * - g 是序列前缀：在超时内接 d / l / s 才跳转，接其他键时序列作废、该键按单键重新判断。
 * - 按住不放的自动重复只对焦点移动有效，避免 s / e / z 被连续触发来回切换。
 */
export function resolveShortcut(
  state: ShortcutState,
  event: ShortcutKeyEvent,
  context: ShortcutContext,
): ShortcutResolution {
  if (event.isComposing) return idle();
  const modified = Boolean(event.metaKey || event.ctrlKey || event.altKey);

  if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
    if (context.typing || context.blocked) return idle();
    return idle("search");
  }
  if (modified || context.typing || context.blocked) return idle();

  const key = event.key;
  const letter = key.length === 1 && !event.shiftKey ? key.toLowerCase() : null;

  if (state.pending === "g" && context.now - state.pendingAt <= SHORTCUT_SEQUENCE_TIMEOUT_MS) {
    const target = letter ? GO_TARGETS[letter] : undefined;
    if (target) return idle(target);
    // 序列作废后，这一键按普通按键重新判断（例如 g 之后按 j 仍然移动焦点）
    return resolveShortcut(INITIAL_SHORTCUT_STATE, event, context);
  }

  if (key === "?") return idle("help");
  if (key === "/") return idle("search");
  if (letter === "g") {
    if (event.repeat) return idle();
    return { state: { pending: "g", pendingAt: context.now }, command: null, preventDefault: true };
  }

  const arrow = ARROW_KEYS[key];
  if (arrow) return context.itemFocused ? idle(arrow) : idle();

  const single = letter ? SINGLE_KEYS[letter] : undefined;
  if (!single) return idle();
  if (event.repeat && isShortcutItemCommand(single)) return idle(null, true);
  return idle(single);
}

/** 只认这些 input 类型为“正在输入”；复选框、按钮等仍允许 j / k 继续移动。 */
const TEXT_INPUT_TYPES = new Set([
  "",
  "text",
  "search",
  "email",
  "url",
  "tel",
  "password",
  "number",
  "date",
  "datetime-local",
  "month",
  "time",
  "week",
]);

/** 自己消费方向键或字母键的复合控件角色。 */
const KEY_OWNING_ROLES = new Set([
  "textbox",
  "searchbox",
  "combobox",
  "listbox",
  "option",
  "menu",
  "menuitem",
  "menuitemradio",
  "menuitemcheckbox",
  "slider",
  "spinbutton",
  "tree",
  "grid",
]);

export type ShortcutTargetLike = {
  tagName?: string;
  type?: string;
  isContentEditable?: boolean;
  getAttribute?: (name: string) => string | null;
};

/** 焦点元素是否在“打字”或者自己处理按键：是则单键快捷键全部让路。 */
export function isTypingTarget(target: ShortcutTargetLike | null | undefined): boolean {
  if (!target || typeof target.tagName !== "string") return false;
  const tag = target.tagName.toUpperCase();
  if (target.isContentEditable) return true;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") return TEXT_INPUT_TYPES.has((target.type ?? "").toLowerCase());
  const role = target.getAttribute?.("role")?.trim().toLowerCase();
  return role ? KEY_OWNING_ROLES.has(role) : false;
}

/**
 * j / k 的下一处焦点。`current` 为 -1 表示焦点不在任何一项上：
 * 向后从第一项开始、向前从最后一项开始。到头不循环，停在边上。
 */
export function nextFocusIndex(
  current: number,
  count: number,
  command: Extract<ShortcutCommand, "focus-next" | "focus-prev">,
): number {
  if (count <= 0) return -1;
  if (current < 0 || current >= count) return command === "focus-next" ? 0 : count - 1;
  return command === "focus-next" ? Math.min(count - 1, current + 1) : Math.max(0, current - 1);
}

/**
 * 画廊按数据顺序（data-shortcut-order）移动焦点：瀑布流的 DOM 是按列排的，
 * 直接用 DOM 顺序会先走完第一列再跳到第二列。全部带序号时按序号排，否则保留 DOM 顺序。
 */
export function orderShortcutItems<T>(items: readonly T[], orderOf: (item: T) => number | null): T[] {
  const orders = items.map(orderOf);
  if (orders.some((order) => order === null || !Number.isFinite(order))) return [...items];
  return items
    .map((item, index) => ({ item, order: orders[index] as number, index }))
    .sort((left, right) => left.order - right.order || left.index - right.index)
    .map(({ item }) => item);
}

export type ShortcutHelpEntry = { keys: readonly string[]; label: string };
export type ShortcutHelpGroup = { title: string; entries: readonly ShortcutHelpEntry[] };

/** 帮助浮层的内容；与 resolveShortcut 的映射一一对应（测试会核对）。 */
export const SHORTCUT_HELP: readonly ShortcutHelpGroup[] = [
  {
    title: "全局",
    entries: [
      { keys: ["⌘", "K"], label: "搜索收藏（Windows / Linux 用 Ctrl K）" },
      { keys: ["/"], label: "搜索收藏" },
      { keys: ["g", "d"], label: "前往发现" },
      { keys: ["g", "l"], label: "前往收藏库" },
      { keys: ["g", "s"], label: "前往 Space" },
      { keys: ["?"], label: "打开这份快捷键说明" },
    ],
  },
  {
    title: "画廊、列表与详情页",
    entries: [
      { keys: ["j"], label: "下一条（焦点在列表里时也可用 ↓ →）" },
      { keys: ["k"], label: "上一条（焦点在列表里时也可用 ↑ ←）" },
      { keys: ["o"], label: "打开原站" },
      { keys: ["s"], label: "星标 / 取消星标" },
      { keys: ["e"], label: "标记已看" },
      { keys: ["z"], label: "推迟到下周再提醒" },
    ],
  },
];
