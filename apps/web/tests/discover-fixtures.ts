import type { LibrarySite } from "../lib/library-contract.ts";

/** 发现页测试共用的收藏样本：默认是一条从未打开的工具站。 */
export function siteFixture(overrides: Partial<LibrarySite> = {}): LibrarySite {
  return {
    id: "site-1",
    name: "VirusTotal：用几十家杀毒引擎同时检测文件和网址",
    originalUrl: "https://www.virustotal.com/gui/home",
    identityUrl: "https://virustotal.com/gui/home",
    summary: null,
    description: null,
    faviconUrl: null,
    previewUrl: null,
    category: { id: "cat-1", name: "工具", isDefault: false, icon: "wrench" },
    tags: [],
    pinned: false,
    source: "manual",
    analysisStatus: "complete",
    analysisPhase: null,
    version: 1,
    createdAt: "2026-10-01T08:00:00",
    updatedAt: "2026-10-01T08:00:00",
    kind: "website",
    kindSource: "auto",
    openCount: 0,
    lastOpenedAt: null,
    seenAt: null,
    starred: false,
    starredAt: null,
    resurfaceMuted: false,
    meta: {},
    ...overrides,
  };
}
