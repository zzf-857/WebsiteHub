export type LibraryAutoLoadMode = "hidden" | "sentinel" | "fallback";

export function libraryAutoLoadMode(
  hasMore: boolean,
  observerSupported: boolean | null,
): LibraryAutoLoadMode {
  if (!hasMore) return "hidden";
  return observerSupported === false ? "fallback" : "sentinel";
}

type LibraryPageLoadState = {
  nextCursor: string | null;
  loading: boolean;
  inFlight: boolean;
  failedCursor: string | null;
};

export function canStartLibraryPageLoad(
  state: LibraryPageLoadState,
): state is LibraryPageLoadState & { nextCursor: string } {
  return Boolean(
    state.nextCursor
    && !state.loading
    && !state.inFlight
    && state.failedCursor !== state.nextCursor,
  );
}
