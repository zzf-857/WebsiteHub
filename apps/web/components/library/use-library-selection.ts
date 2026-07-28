"use client";

import { useCallback, useMemo, useState } from "react";
import type {
  LibrarySite,
  LibrarySiteSelectionItem,
} from "@/lib/library-contract";
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
  coverage: "custom" | "matching";
  sites: Map<string, LibrarySiteSelectionItem>;
};

type UseLibrarySelectionOptions = {
  scope: string;
  pinnedSites: readonly LibrarySite[];
  regularSites: readonly LibrarySite[];
};

export function useLibrarySelection({
  scope,
  pinnedSites,
  regularSites,
}: UseLibrarySelectionOptions) {
  const [selection, setSelection] = useState<SelectionState>({
    scope: null,
    active: false,
    coverage: "custom",
    sites: new Map<string, LibrarySiteSelectionItem>(),
  });

  const selectionMode = selection.active && selection.scope === scope;
  const selectedSiteIds = useMemo(
    () => selection.scope === scope ? new Set(selection.sites.keys()) : EMPTY_SELECTION,
    [scope, selection.scope, selection.sites],
  );
  const loadedSites = useMemo(() => {
    const known = new Set<string>();
    return [...pinnedSites, ...regularSites].filter((site) => {
      if (known.has(site.id)) return false;
      known.add(site.id);
      return true;
    });
  }, [pinnedSites, regularSites]);
  const selectedSites = useMemo(
    () => selection.scope === scope ? [...selection.sites.values()] : [],
    [scope, selection.scope, selection.sites],
  );
  const selectableLoadedSites = selectableLoadedLibrarySites(loadedSites);
  const allLoadedSelected = areAllLoadedLibrarySitesSelected(
    selectableLoadedSites,
    selectedSiteIds,
  );
  const allMatchingSelected = selectionMode && selection.coverage === "matching";

  const clearSelection = useCallback(() => {
    setSelection({
      scope: null,
      active: false,
      coverage: "custom",
      sites: new Map<string, LibrarySiteSelectionItem>(),
    });
  }, []);

  const clearSelectedSites = useCallback(() => {
    setSelection({
      scope,
      active: true,
      coverage: "custom",
      sites: new Map<string, LibrarySiteSelectionItem>(),
    });
  }, [scope]);

  const setSelectionMode = useCallback((active: boolean) => {
    setSelection((current) => ({
      scope,
      active,
      coverage: active && current.scope === scope ? current.coverage : "custom",
      sites: active && current.scope === scope
        ? current.sites
        : new Map<string, LibrarySiteSelectionItem>(),
    }));
  }, [scope]);

  const toggleSiteSelection = useCallback((siteId: string) => {
    const site = loadedSites.find((candidate) => candidate.id === siteId);
    if (!site) return;
    setSelection((current) => {
      const next = current.scope === scope
        ? new Map(current.sites)
        : new Map<string, LibrarySiteSelectionItem>();
      if (next.has(siteId)) next.delete(siteId);
      else next.set(siteId, site);
      return { scope, active: true, coverage: "custom", sites: next };
    });
  }, [loadedSites, scope]);

  const toggleAllLoadedSites = useCallback(() => {
    setSelection((current) => {
      const currentIds = current.scope === scope
        ? new Set(current.sites.keys())
        : new Set<string>();
      const nextIds = nextAllLoadedLibrarySiteIds(selectableLoadedSites, currentIds);
      const nextSites = current.scope === scope
        ? new Map(current.sites)
        : new Map<string, LibrarySiteSelectionItem>();
      for (const site of selectableLoadedSites) {
        if (nextIds.has(site.id)) nextSites.set(site.id, site);
        else nextSites.delete(site.id);
      }
      return { scope, active: true, coverage: "custom", sites: nextSites };
    });
  }, [scope, selectableLoadedSites]);

  const selectAllMatchingSites = useCallback((sites: readonly LibrarySiteSelectionItem[]) => {
    setSelection({
      scope,
      active: true,
      coverage: "matching",
      sites: new Map<string, LibrarySiteSelectionItem>(
        sites.map((site) => [site.id, site]),
      ),
    });
  }, [scope]);

  const retainVisibleSelection = useCallback((visibleSites: readonly LibrarySite[]) => {
    setSelection((current) => {
      if (!current.active || current.scope !== scope) return current;
      if (current.coverage === "matching") return current;
      const retained = retainLoadedLibrarySiteIds(visibleSites, new Set(current.sites.keys()));
      const sites = new Map<string, LibrarySiteSelectionItem>();
      for (const siteId of retained) {
        const snapshot = current.sites.get(siteId);
        if (snapshot) sites.set(siteId, snapshot);
      }
      return { ...current, sites };
    });
  }, [scope]);

  return {
    allLoadedSelected,
    allMatchingSelected,
    clearSelectedSites,
    clearSelection,
    loadedSiteCount: loadedSites.length,
    retainVisibleSelection,
    selectedSiteIds,
    selectedSites,
    selectionMode,
    selectAllMatchingSites,
    setSelectionMode,
    toggleAllLoadedSites,
    toggleSiteSelection,
  };
}
