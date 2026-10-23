"use client";

import { FolderInput } from "lucide-react";
import { useEffect, useState } from "react";

import { getSiteImportOrigins, importOriginsSummary } from "@/lib/import-origins";

// 书签来源（Q55）：这条收藏来自哪些浏览器书签目录。只读的一行；
// 不是导入来的、或读取失败时整行不出现——来源是补充信息，不打断详情页。

type ImportOriginsLineProps = {
  siteId: string;
};

export function ImportOriginsLine({ siteId }: Readonly<ImportOriginsLineProps>) {
  const [summary, setSummary] = useState<{ siteId: string; text: string; full: string } | null>(
    null,
  );

  useEffect(() => {
    const controller = new AbortController();
    void getSiteImportOrigins(siteId, controller.signal)
      .then((origins) => {
        const next = importOriginsSummary(origins);
        setSummary(next ? { siteId, ...next } : null);
      })
      .catch(() => {
        if (!controller.signal.aborted) setSummary(null);
      });
    return () => controller.abort();
  }, [siteId]);

  if (!summary || summary.siteId !== siteId) return null;

  return (
    <p className="sd-meta-line" data-testid="sd-import-origins" title={summary.full}>
      <span className="sd-meta-host">
        <FolderInput size={14} aria-hidden="true" />
        {summary.text}
      </span>
    </p>
  );
}
