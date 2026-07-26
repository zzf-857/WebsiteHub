/**
 * 「一键全部打开」的纯逻辑：决定每一轮该打开哪些地址。
 *
 * 单独拎出来是因为它有一个容易写错、又必须正确的性质：**重试只能重试失败的**。
 * 之前的实现每次都遍历全部地址，用户在弹窗被拦截后点一次重试，
 * 已经成功打开的标签会被再开一遍——站点越多越明显。
 */

export type OpenAllMode = "tabs" | "current";

export type OpenAllAttempt = {
  /** 本轮实际尝试打开的地址 */
  attempted: string[];
  /** 其中被浏览器拦截的 */
  blocked: string[];
  /** 累计已成功打开的，用于「已打开 N / 共 M」 */
  opened: string[];
};

/**
 * 下一轮该尝试哪些地址。
 *
 * 首次是全部；之后只剩上一轮被拦截的那些。返回空数组表示没有剩余工作，
 * 调用方据此收起弹层而不是再发一轮空操作。
 */
export function pendingTargets(all: readonly string[], opened: readonly string[]): string[] {
  if (opened.length === 0) return [...all];
  const done = new Set(opened);
  return all.filter((url) => !done.has(url));
}

/**
 * 合并一轮尝试的结果。
 *
 * `opened` 用集合去重：同一个地址在 Space 里出现两次时（成员理论上唯一，
 * 但这里不依赖那个前提），不该被计成两次成功。
 */
export function mergeAttempt(
  previous: readonly string[],
  attempted: readonly string[],
  blocked: readonly string[],
): string[] {
  const blockedSet = new Set(blocked);
  const merged = new Set(previous);
  for (const url of attempted) {
    if (!blockedSet.has(url)) merged.add(url);
  }
  return [...merged];
}

/**
 * 「在当前窗口依次打开」时，本页要跳转到哪个地址。
 *
 * 行为与文案必须对齐：这个模式**并不是**在同一个标签里逐个走完，
 * 而是「第一个占用当前标签、其余开新标签」。返回 null 表示没有可跳转的目标
 * （全部被拦截，或列表为空），此时不能让本页跳走——跳走了脚本就终止，
 * 剩下的重试机会也一起没了。
 */
export function currentWindowTarget(
  all: readonly string[],
  opened: readonly string[],
): string | null {
  const first = all[0];
  if (!first) return null;
  // 第一个还没打开过才轮到本页跳转；否则本页应该留在原地。
  return opened.includes(first) ? null : first;
}

/** 「在当前窗口依次打开」时，要开进新标签的那些（即除第一个之外的待办）。 */
export function currentWindowNewTabTargets(
  all: readonly string[],
  opened: readonly string[],
): string[] {
  return pendingTargets(all.slice(1), opened);
}
