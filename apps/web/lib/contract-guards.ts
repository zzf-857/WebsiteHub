/**
 * 契约层共用的校验原语。
 *
 * 四个 `*-contract.ts` 此前各自实现了同一套 `record` / `text` / `version` …，
 * 函数体**逐字节相同**，唯一的差别是抛出的错误类。九个原语乘上四个文件，
 * 约三十份重复实现——改一处校验规则要记得改四遍，漏一处就是行为不一致。
 *
 * 每个模块保留自己的错误类型（测试与调用方都按类型区分来源），
 * 所以这里做成「传入错误工厂、返回一组绑定好的守卫」，而不是导出裸函数。
 */

export type ContractErrorFactory = (message: string) => Error;

type JsonRecord = Record<string, unknown>;

export type ContractGuards = {
  record: (value: unknown, path: string) => JsonRecord;
  text: (value: unknown, path: string) => string;
  boundedText: (value: unknown, path: string, maximum: number) => string;
  nullableText: (value: unknown, path: string) => string | null;
  identifier: (value: unknown, path: string) => string;
  boolean: (value: unknown, path: string) => boolean;
  count: (value: unknown, path: string) => number;
  version: (value: unknown, path: string) => number;
  absoluteWebUrl: (value: unknown, path: string) => string;
  nullableWebUrl: (value: unknown, path: string) => string | null;
  isoDate: (value: unknown, path: string) => string;
  literal: <const Values extends readonly string[]>(
    value: unknown,
    path: string,
    values: Values,
  ) => Values[number];
  listPayload: (value: unknown, path: string) => unknown[];
};

export function createContractGuards(fail: ContractErrorFactory): ContractGuards {
  const record = (value: unknown, path: string): JsonRecord => {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      throw fail(`${path} 必须是对象`);
    }
    return value as JsonRecord;
  };

  const text = (value: unknown, path: string): string => {
    if (typeof value !== "string" || !value.trim()) {
      throw fail(`${path} 必须是非空字符串`);
    }
    return value.trim();
  };

  const boundedText = (value: unknown, path: string, maximum: number): string => {
    const normalized = text(value, path);
    // 按码点计数而不是 UTF-16 长度：一个中文字符是一个字，不是两个。
    if (Array.from(normalized).length > maximum) {
      throw fail(`${path} 不能超过 ${maximum} 个字符`);
    }
    return normalized;
  };

  const nullableText = (value: unknown, path: string): string | null => {
    if (value === null || value === undefined) return null;
    if (typeof value !== "string") throw fail(`${path} 必须是字符串或 null`);
    return value.trim() || null;
  };

  const identifier = (value: unknown, path: string): string => {
    if (typeof value === "string" && value.trim()) return value.trim();
    // 后端历史上有过整数主键，这里一并容忍，避免调用方各自做兼容。
    if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return String(value);
    throw fail(`${path} 必须是有效标识符`);
  };

  const boolean = (value: unknown, path: string): boolean => {
    if (typeof value !== "boolean") throw fail(`${path} 必须是布尔值`);
    return value;
  };

  const count = (value: unknown, path: string): number => {
    if (!Number.isSafeInteger(value) || (value as number) < 0) {
      throw fail(`${path} 必须是非负整数`);
    }
    return value as number;
  };

  const version = (value: unknown, path: string): number => {
    if (!Number.isSafeInteger(value) || (value as number) < 1) {
      throw fail(`${path} 必须是正整数`);
    }
    return value as number;
  };

  const absoluteWebUrl = (value: unknown, path: string): string => {
    const candidate = text(value, path);
    try {
      const parsed = new URL(candidate);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("protocol");
    } catch {
      throw fail(`${path} 必须是 HTTP(S) URL`);
    }
    return candidate;
  };

  const nullableWebUrl = (value: unknown, path: string): string | null => {
    if (value === null || value === undefined) return null;
    return absoluteWebUrl(value, path);
  };

  const isoDate = (value: unknown, path: string): string => {
    const candidate = text(value, path);
    if (Number.isNaN(Date.parse(candidate))) throw fail(`${path} 必须是有效日期`);
    return candidate;
  };

  const literal = <const Values extends readonly string[]>(
    value: unknown,
    path: string,
    values: Values,
  ): Values[number] => {
    if (typeof value !== "string" || !values.some((candidate) => candidate === value)) {
      throw fail(`${path} 不是受支持的值`);
    }
    return value as Values[number];
  };

  // 后端有的端点回裸数组、有的回 { items: [...] }，两种都收。
  const listPayload = (value: unknown, path: string): unknown[] => {
    if (Array.isArray(value)) return value;
    const candidate = record(value, path);
    if (!Array.isArray(candidate.items)) throw fail(`${path}.items 必须是数组`);
    return candidate.items;
  };

  return {
    record,
    text,
    boundedText,
    nullableText,
    identifier,
    boolean,
    count,
    version,
    absoluteWebUrl,
    nullableWebUrl,
    isoDate,
    literal,
    listPayload,
  };
}
