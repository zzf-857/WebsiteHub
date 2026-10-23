import { createContractGuards } from "./contract-guards.ts";

export const PORTABILITY_MAX_BYTES = 64 * 1024 * 1024;
export const PORTABILITY_SECTIONS = [
  "categories", "tags", "sites", "spaces", "feeds", "conversations",
] as const;
export type PortabilitySection = typeof PORTABILITY_SECTIONS[number];
export const PORTABILITY_LABELS: Record<PortabilitySection, string> = {
  categories: "分类", tags: "标签", sites: "收藏", spaces: "Space", feeds: "订阅", conversations: "站内对话",
};
export type PortabilityCounts = Record<PortabilitySection, {
  create: number;
  merge: number;
  reuse: number;
  skipped: number;
}>;
export const PORTABILITY_SAMPLE_LABELS = {
  categories_create: "新增分类", categories_reuse: "复用分类",
  tags_create: "新增标签", tags_reuse: "复用标签",
  sites_create: "新增收藏", sites_merge: "合并收藏", sites_skip_invalid: "跳过的收藏",
  spaces_create: "新增 Space", spaces_merge: "合并 Space",
  feeds_create: "新增订阅", feeds_skip_existing: "已有订阅",
  conversations_skipped: "跳过的站内对话",
} as const;
export type PortabilitySampleKey = keyof typeof PORTABILITY_SAMPLE_LABELS;
export type PortabilitySample = { name: string; url: string | null };
export type PortabilityPreview = {
  id: string;
  status: "previewed";
  expiresAt: string;
  payloadSha256: string;
  counts: PortabilityCounts;
  warnings: { code: string; message: string }[];
  samples: Partial<Record<PortabilitySampleKey, PortabilitySample[]>>;
};
export type PortabilityResult = {
  id: string;
  status: "applied";
  appliedAt: string;
  created: Record<PortabilitySection, number>;
  merged: Record<PortabilitySection, number>;
};

export class PortabilityContractError extends Error {
  constructor(message: string) {
    super(`备份接口返回格式异常：${message}`);
    this.name = "PortabilityContractError";
  }
}

const guards = createContractGuards((message) => new PortabilityContractError(message));

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new PortabilityContractError(`${path} 必须是数组`);
  return value;
}

function optionalCount(raw: Record<string, unknown>, key: string, path: string): number {
  return raw[key] === undefined ? 0 : guards.count(raw[key], `${path}.${key}`);
}

export function normalizePortabilityPreview(value: unknown, path = "preview"): PortabilityPreview {
  const raw = guards.record(value, path);
  const counts = guards.record(raw.counts, `${path}.counts`);
  const normalizedCounts = {} as PortabilityCounts;
  for (const section of PORTABILITY_SECTIONS) {
    const sectionPath = `${path}.counts.${section}`;
    const entry = guards.record(counts[section], sectionPath);
    normalizedCounts[section] = {
      create: optionalCount(entry, "create", sectionPath),
      merge: optionalCount(entry, "merge", sectionPath),
      reuse: optionalCount(entry, "reuse", sectionPath),
      skipped: optionalCount(entry, "skipped", sectionPath)
        + optionalCount(entry, "skip_invalid", sectionPath)
        + optionalCount(entry, "skip_existing", sectionPath),
    };
  }
  const samples: PortabilityPreview["samples"] = {};
  const sampleSource = raw.samples === undefined ? {} : guards.record(raw.samples, `${path}.samples`);
  for (const key of Object.keys(PORTABILITY_SAMPLE_LABELS) as PortabilitySampleKey[]) {
    if (sampleSource[key] === undefined) continue;
    samples[key] = array(sampleSource[key], `${path}.samples.${key}`).map((item, index) => {
      const itemPath = `${path}.samples.${key}[${index}]`;
      const sample = guards.record(item, itemPath);
      return {
        name: guards.text(sample.name, `${itemPath}.name`),
        // 无效地址样例仅作为文字展示，不把用户上传的值用作链接。
        url: guards.nullableText(sample.url, `${itemPath}.url`),
      };
    }).slice(0, 20);
  }
  const checksum = guards.text(raw.payload_sha256, `${path}.payload_sha256`);
  if (!/^[a-f0-9]{64}$/u.test(checksum)) {
    throw new PortabilityContractError(`${path}.payload_sha256 必须是 SHA-256 摘要`);
  }
  return {
    id: guards.identifier(raw.id, `${path}.id`),
    status: guards.literal(raw.status, `${path}.status`, ["previewed"]),
    expiresAt: guards.isoDate(raw.expires_at, `${path}.expires_at`),
    payloadSha256: checksum,
    counts: normalizedCounts,
    warnings: raw.warnings === undefined ? [] : array(raw.warnings, `${path}.warnings`).map((item, index) => {
      const warningPath = `${path}.warnings[${index}]`;
      const warning = guards.record(item, warningPath);
      return {
        code: guards.text(warning.code, `${warningPath}.code`),
        message: guards.text(warning.message, `${warningPath}.message`),
      };
    }),
    samples,
  };
}

export function normalizePortabilityResult(value: unknown, path = "result"): PortabilityResult {
  const raw = guards.record(value, path);
  const normalizeTotals = (value: unknown, totalsPath: string): Record<PortabilitySection, number> => {
    const source = guards.record(value, totalsPath);
    return Object.fromEntries(PORTABILITY_SECTIONS.map((section) => [
      section, optionalCount(source, section, totalsPath),
    ])) as Record<PortabilitySection, number>;
  };
  return {
    id: guards.identifier(raw.id, `${path}.id`),
    status: guards.literal(raw.status, `${path}.status`, ["applied"]),
    appliedAt: guards.isoDate(raw.applied_at, `${path}.applied_at`),
    created: normalizeTotals(raw.created, `${path}.created`),
    merged: normalizeTotals(raw.merged, `${path}.merged`),
  };
}

export function portabilityFileError(file: Pick<File, "name" | "size">): string | null {
  if (!file.name.toLowerCase().endsWith(".json")) return "请选择 WebHub 导出的 .json 备份文件。";
  if (file.size > PORTABILITY_MAX_BYTES) return "备份超过 64 MiB 上限，请选择较小的备份文件。";
  if (file.size === 0) return "备份文件为空，请重新选择。";
  return null;
}
