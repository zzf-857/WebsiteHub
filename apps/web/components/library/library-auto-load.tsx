"use client";

import { useEffect, useRef, useSyncExternalStore } from "react";
import { Spinner } from "@/components/react-bits/spinner";
import { libraryAutoLoadMode } from "@/lib/library-pagination";

const LIBRARY_LOAD_AHEAD_MARGIN = "0px 0px 640px 0px";
const subscribeToObserverSupport = () => () => undefined;
const observerSupportSnapshot = () => typeof IntersectionObserver !== "undefined";
const serverObserverSupportSnapshot = () => true;

type LibraryAutoLoadProps = {
  hasMore: boolean;
  loading: boolean;
  loadingLabel: string;
  fallbackLabel: string;
  onLoadMore: () => void | Promise<void>;
};

export function LibraryAutoLoad({
  hasMore,
  loading,
  loadingLabel,
  fallbackLabel,
  onLoadMore,
}: LibraryAutoLoadProps) {
  const sentinelRef = useRef<HTMLDivElement>(null);
  const loadMoreRef = useRef(onLoadMore);
  const observerSupported = useSyncExternalStore(
    subscribeToObserverSupport,
    observerSupportSnapshot,
    serverObserverSupportSnapshot,
  );

  useEffect(() => {
    loadMoreRef.current = onLoadMore;
  }, [onLoadMore]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!observerSupported || !sentinel || !hasMore || loading) {
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          void loadMoreRef.current();
        }
      },
      { rootMargin: LIBRARY_LOAD_AHEAD_MARGIN, threshold: 0 },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, loading, observerSupported]);

  const mode = libraryAutoLoadMode(hasMore, observerSupported);
  if (mode === "hidden") return null;
  if (mode === "fallback") {
    return (
      <button
        className="library-auto-load-fallback"
        type="button"
        disabled={loading}
        onClick={() => void loadMoreRef.current()}
      >
        {loading && <Spinner size={16} />}
        {loading ? loadingLabel : fallbackLabel}
      </button>
    );
  }

  return (
    <div
      ref={sentinelRef}
      className="library-auto-load"
      data-loading={loading || undefined}
      aria-busy={loading}
      aria-live="polite"
    >
      {loading && (
        <>
          <Spinner size={16} />
          <span className="sr-only">{loadingLabel}</span>
        </>
      )}
    </div>
  );
}
