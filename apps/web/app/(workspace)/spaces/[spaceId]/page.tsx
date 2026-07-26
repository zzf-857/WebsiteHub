import type { Metadata } from "next";

import { SpaceWorkspace } from "@/components/spaces/space-workspace";

export const metadata: Metadata = { title: "Space | WebHub" };

type SpacePageProps = {
  params: Promise<{ spaceId: string }>;
};

export default async function SpacePage({ params }: Readonly<SpacePageProps>) {
  const { spaceId } = await params;
  return <SpaceWorkspace key={spaceId} initialSpaceId={spaceId} />;
}
