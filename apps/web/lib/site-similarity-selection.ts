type SiteSimilaritySelectionMember = Readonly<{ id: string }>;

type SiteSimilaritySelectionGroup = Readonly<{
  members: readonly SiteSimilaritySelectionMember[];
}>;

export type SiteSimilarityKeepAction = Readonly<{
  label: "保留所选" | "全部保留";
  keepSiteIds: string[];
}>;

export function normalizeSiteSimilarityKeepSelection(
  group: SiteSimilaritySelectionGroup,
  requestedKeepSiteIds: readonly string[],
): string[] {
  const requestedIds = new Set(requestedKeepSiteIds);
  const orderedKeepSiteIds = group.members
    .filter((member) => requestedIds.has(member.id))
    .map((member) => member.id);

  return orderedKeepSiteIds.length === group.members.length
    ? []
    : orderedKeepSiteIds;
}

export function toggleSiteSimilarityKeepSelection(
  group: SiteSimilaritySelectionGroup,
  currentKeepSiteIds: readonly string[],
  siteId: string,
): string[] {
  if (!group.members.some((member) => member.id === siteId)) {
    throw new TypeError("只能切换当前相似网站分组中的网站");
  }
  const nextKeepSiteIds = new Set(currentKeepSiteIds);
  if (nextKeepSiteIds.has(siteId)) nextKeepSiteIds.delete(siteId);
  else nextKeepSiteIds.add(siteId);
  return normalizeSiteSimilarityKeepSelection(group, [...nextKeepSiteIds]);
}

export function siteSimilarityKeepAction(
  group: SiteSimilaritySelectionGroup,
  currentKeepSiteIds: readonly string[],
): SiteSimilarityKeepAction {
  const keepSiteIds = normalizeSiteSimilarityKeepSelection(group, currentKeepSiteIds);
  return {
    label: keepSiteIds.length > 0 ? "保留所选" : "全部保留",
    keepSiteIds,
  };
}
