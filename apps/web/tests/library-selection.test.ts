import assert from "node:assert/strict";
import test from "node:test";

import { MAX_LIBRARY_BULK_DELETE_SITES } from "../lib/library-contract.ts";
import {
  areAllLoadedLibrarySitesSelected,
  retainLoadedLibrarySiteIds,
  selectableLoadedLibrarySites,
  toggleAllLoadedLibrarySites,
} from "../lib/library-selection.ts";

test("select-all is explicitly bounded to the currently loaded selectable sites", () => {
  const loaded = Array.from(
    { length: MAX_LIBRARY_BULK_DELETE_SITES + 7 },
    (_, index) => ({ id: `site-${index}` }),
  );
  const selectable = selectableLoadedLibrarySites(loaded);

  assert.equal(selectable.length, MAX_LIBRARY_BULK_DELETE_SITES);
  const selected = toggleAllLoadedLibrarySites(selectable, new Set());
  assert.equal(selected.size, MAX_LIBRARY_BULK_DELETE_SITES);
  assert.equal(areAllLoadedLibrarySitesSelected(selectable, selected), true);
  assert.equal(selected.has(`site-${MAX_LIBRARY_BULK_DELETE_SITES}`), false);

  const cleared = toggleAllLoadedLibrarySites(selectable, selected);
  assert.equal(cleared.size, 0);
  assert.equal(areAllLoadedLibrarySitesSelected(selectable, cleared), false);
});

test("select-all remains unchecked for a partial visible selection", () => {
  const selectable = selectableLoadedLibrarySites([
    { id: "site-1" },
    { id: "site-2" },
  ]);
  assert.equal(
    areAllLoadedLibrarySitesSelected(selectable, new Set(["site-1"])),
    false,
  );
});

test("same-scope refresh retains visible selections and prunes hidden ids", () => {
  const retained = retainLoadedLibrarySiteIds(
    [{ id: "site-2" }, { id: "site-3" }],
    new Set(["site-1", "site-2"]),
  );
  assert.deepEqual([...retained], ["site-2"]);
});
