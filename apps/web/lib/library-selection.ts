type SelectableSite = { id: string };

export function selectableLoadedLibrarySites<T extends SelectableSite>(
  loadedSites: readonly T[],
): T[] {
  return [...loadedSites];
}

export function areAllLoadedLibrarySitesSelected(
  selectableSites: readonly SelectableSite[],
  selectedSiteIds: ReadonlySet<string>,
): boolean {
  return selectableSites.length > 0
    && selectableSites.every((site) => selectedSiteIds.has(site.id));
}

export function toggleAllLoadedLibrarySites(
  selectableSites: readonly SelectableSite[],
  selectedSiteIds: ReadonlySet<string>,
): Set<string> {
  const next = new Set(selectedSiteIds);
  if (areAllLoadedLibrarySitesSelected(selectableSites, selectedSiteIds)) {
    for (const site of selectableSites) next.delete(site.id);
  } else {
    for (const site of selectableSites) next.add(site.id);
  }
  return next;
}

export function retainLoadedLibrarySiteIds(
  loadedSites: readonly SelectableSite[],
  selectedSiteIds: ReadonlySet<string>,
): Set<string> {
  const loadedIds = new Set(loadedSites.map((site) => site.id));
  return new Set([...selectedSiteIds].filter((siteId) => loadedIds.has(siteId)));
}
