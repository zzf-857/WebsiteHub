import { MAX_LIBRARY_BULK_DELETE_SITES } from "./library-contract.ts";

type SelectableSite = { id: string };

export function selectableLoadedLibrarySites<T extends SelectableSite>(
  loadedSites: readonly T[],
): T[] {
  return loadedSites.slice(0, MAX_LIBRARY_BULK_DELETE_SITES);
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
  if (areAllLoadedLibrarySitesSelected(selectableSites, selectedSiteIds)) return new Set();
  return new Set(selectableSites.map((site) => site.id));
}

export function retainLoadedLibrarySiteIds(
  loadedSites: readonly SelectableSite[],
  selectedSiteIds: ReadonlySet<string>,
): Set<string> {
  const loadedIds = new Set(loadedSites.map((site) => site.id));
  return new Set([...selectedSiteIds].filter((siteId) => loadedIds.has(siteId)));
}
