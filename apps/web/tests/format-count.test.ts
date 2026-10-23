import assert from "node:assert/strict";
import test from "node:test";

import { formatCount } from "../lib/format-count.ts";

test("formatCount: thousands separators below 1 万, no k", () => {
  assert.equal(formatCount(0), "0");
  assert.equal(formatCount(999), "999");
  assert.equal(formatCount(1_000), "1,000");
  assert.equal(formatCount(6_100), "6,100");
  assert.equal(formatCount(9_999), "9,999");
});

test("formatCount: 万 / 亿 units truncate to one decimal without rounding up", () => {
  assert.equal(formatCount(10_000), "1 万");
  assert.equal(formatCount(52_300), "5.2 万");
  assert.equal(formatCount(158_000), "15.8 万");
  assert.equal(formatCount(1_200_000), "120 万");
  assert.equal(formatCount(1_234_567), "123.4 万");
  assert.equal(formatCount(99_999_999), "9999.9 万");
  assert.equal(formatCount(100_000_000), "1 亿");
  assert.equal(formatCount(123_456_789), "1.2 亿");
});

test("formatCount: invalid input degrades to 0 and fractions are floored", () => {
  assert.equal(formatCount(12.9), "12");
  assert.equal(formatCount(-3), "0");
  assert.equal(formatCount(Number.NaN), "0");
  assert.equal(formatCount(Number.POSITIVE_INFINITY), "0");
});
