import type { Metadata } from "next";

import { DataPortabilityWorkspace } from "@/components/settings/data-portability-workspace";

export const metadata: Metadata = { title: "数据导出与恢复 · WebHub" };

export default function DataPortabilityPage() {
  return <DataPortabilityWorkspace />;
}
