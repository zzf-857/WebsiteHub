import assert from "node:assert/strict";
import test from "node:test";

import {
  areAllLoadedLibrarySitesSelected,
  retainLoadedLibrarySiteIds,
  selectableLoadedLibrarySites,
  toggleAllLoadedLibrarySites,
} from "../lib/library-selection.ts";

test("select-all includes every currently loaded site", () => {
  const loaded = Array.from(
    { length: 107 },
    (_, index) => ({ id: `site-${index}` }),
  );
  const selectable = selectableLoadedLibrarySites(loaded);

  assert.equal(selectable.length, loaded.length);
  const selected = toggleAllLoadedLibrarySites(selectable, new Set(["offscreen-site"]));
  assert.equal(selected.size, loaded.length + 1);
  assert.equal(areAllLoadedLibrarySitesSelected(selectable, selected), true);
  assert.equal(selected.has("site-106"), true);

  const cleared = toggleAllLoadedLibrarySites(selectable, selected);
  assert.deepEqual([...cleared], ["offscreen-site"]);
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
