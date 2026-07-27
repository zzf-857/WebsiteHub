"use client";

import { useCallback, useMemo, useState } from "react";
import { MAX_LIBRARY_BULK_DELETE_SITES, type LibrarySite } from "@/lib/library-contract";
import {
  areAllLoadedLibrarySitesSelected,
  retainLoadedLibrarySiteIds,
  selectableLoadedLibrarySites,
  toggleAllLoadedLibrarySites as nextAllLoadedLibrarySiteIds,
} from "@/lib/library-selection";

const EMPTY_SELECTION = new Set<string>();

type SelectionState = {
  scope: string | null;
  active: boolean;
  siteIds: Set<string>;
};

type UseLibrarySelectionOptions = {
  scope: string;
  pinnedSites: readonly LibrarySite[];
  regularSites: readonly LibrarySite[];
  onNotice: (message: string) => void;
};

export function useLibrarySelection({
  scope,
  pinnedSites,
  regularSites,
  onNotice,
}: UseLibrarySelectionOptions) {
  const [selection, setSelection] = useState<SelectionState>({
    scope: null,
    active: false,
    siteIds: new Set(),
  });

  const selectionMode = selection.active && selection.scope === scope;
  const selectedSiteIds = selection.scope === scope ? selection.siteIds : EMPTY_SELECTION;
  const loadedSites = useMemo(() => {
    const known = new Set<string>();
    return [...pinnedSites, ...regularSites].filter((site) => {
      if (known.has(site.id)) return false;
      known.add(site.id);
      return true;
    });
  }, [pinnedSites, regularSites]);
  const selectedSites = useMemo(
    () => loadedSites.filter((site) => selectedSiteIds.has(site.id)),
    [loadedSites, selectedSiteIds],
  );
  const selectableLoadedSites = selectableLoadedLibrarySites(loadedSites);
  const allLoadedSelected = areAllLoadedLibrarySitesSelected(
    selectableLoadedSites,
    selectedSiteIds,
  );

  const clearSelection = useCallback(() => {
    setSelection({ scope: null, active: false, siteIds: new Set() });
  }, []);

  const setSelectionMode = useCallback((active: boolean) => {
    setSelection((current) => ({
      scope,
      active,
      siteIds: active && current.scope === scope ? current.siteIds : new Set(),
    }));
  }, [scope]);

  const toggleSiteSelection = useCallback((siteId: string) => {
    if (!selectedSiteIds.has(siteId) && selectedSites.length >= MAX_LIBRARY_BULK_DELETE_SITES) {
      onNotice(`单次最多选择 ${MAX_LIBRARY_BULK_DELETE_SITES} 个网站`);
      return;
    }
    setSelection(() => {
      const next = new Set(selectedSites.map((site) => site.id));
      if (next.has(siteId)) next.delete(siteId);
      else next.add(siteId);
      return { scope, active: true, siteIds: next };
    });
  }, [onNotice, scope, selectedSiteIds, selectedSites]);

  const toggleAllLoadedSites = useCallback(() => {
    setSelection({
      scope,
      active: true,
      siteIds: nextAllLoadedLibrarySiteIds(selectableLoadedSites, selectedSiteIds),
    });
    if (!allLoadedSelected && loadedSites.length > MAX_LIBRARY_BULK_DELETE_SITES) {
      onNotice(
        `当前已加载 ${loadedSites.length} 个网站，已选择前 ${MAX_LIBRARY_BULK_DELETE_SITES} 个`,
      );
    }
  }, [allLoadedSelected, loadedSites.length, onNotice, scope, selectableLoadedSites, selectedSiteIds]);

  const retainVisibleSelection = useCallback((visibleSites: readonly LibrarySite[]) => {
    setSelection((current) => {
      if (!current.active || current.scope !== scope) return current;
      const retained = retainLoadedLibrarySiteIds(visibleSites, current.siteIds);
      return retained.size === current.siteIds.size
        ? current
        : { ...current, siteIds: retained };
    });
  }, [scope]);

  return {
    allLoadedSelected,
    clearSelection,
    loadedSiteCount: loadedSites.length,
    retainVisibleSelection,
    selectedSiteIds,
    selectedSites,
    selectionMode,
    setSelectionMode,
    toggleAllLoadedSites,
    toggleSiteSelection,
  };
}
