import type { Metadata } from "next";

import { SiteDetailPage } from "@/components/library/detail/site-detail-page";

export const metadata: Metadata = { title: "网站详情 | WebHub" };

type LibrarySitePageProps = {
  params: Promise<{ siteId: string }>;
};

export default async function LibrarySitePage({ params }: Readonly<LibrarySitePageProps>) {
  const { siteId } = await params;
  // key 保证在站点之间跳转（如点击「相关网站」）时客户端状态完全重置
  return <SiteDetailPage key={siteId} siteId={siteId} />;
}
