import assert from "node:assert/strict";
import test from "node:test";

import { PINNED_CHIP_LIMIT, shouldUsePinnedChips } from "../lib/library-pinned.ts";

test("pinned section uses compact chips only below the limit and when fully loaded", () => {
  assert.equal(PINNED_CHIP_LIMIT, 4);
  assert.equal(shouldUsePinnedChips(0, false), false);
  assert.equal(shouldUsePinnedChips(1, false), true);
  assert.equal(shouldUsePinnedChips(3, false), true);
  assert.equal(shouldUsePinnedChips(4, false), false);
  assert.equal(shouldUsePinnedChips(12, false), false);
  assert.equal(shouldUsePinnedChips(2, true), false, "more pages pending keeps the grid");
});
