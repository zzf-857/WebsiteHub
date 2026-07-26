import type { Metadata } from "next";

import { SiteDetail } from "@/components/library/site-detail";

export const metadata: Metadata = { title: "站点详情 | WebHub" };

type LibrarySitePageProps = {
  params: Promise<{ siteId: string }>;
};

export default async function LibrarySitePage({ params }: Readonly<LibrarySitePageProps>) {
  const { siteId } = await params;
  return <SiteDetail key={siteId} siteId={siteId} />;
}
