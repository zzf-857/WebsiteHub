import assert from "node:assert/strict";
import test from "node:test";

import { feedMark } from "../lib/feeds-mark.ts";
import { siteHue } from "../lib/resource-cover.ts";

const feed = { title: "少数派", url: "https://sspai.com/feed", siteUrl: "https://sspai.com/" };

test("feedMark: favicon comes from the site's own origin, hue from the shared cover hash", () => {
  const mark = feedMark(feed);
  assert.equal(mark.letter, "少");
  assert.equal(mark.faviconUrl, "https://sspai.com/favicon.ico");
  assert.equal(mark.hue, siteHue("sspai.com"));
  assert.deepEqual(feedMark({ ...feed, title: "Other" }).hue, mark.hue, "same host keeps the same hue");
});

test("feedMark: no site_url falls back to a letter tile keyed by the feed host", () => {
  const mark = feedMark({ title: "latent space", url: "https://www.latent.space/feed", siteUrl: null });
  assert.equal(mark.faviconUrl, null);
  assert.equal(mark.letter, "L");
  assert.equal(mark.hue, siteHue("latent.space"));
});

test("feedMark: rejects non-http site urls and survives missing feeds", () => {
  assert.equal(feedMark({ ...feed, siteUrl: "javascript:alert(1)" }).faviconUrl, null);
  assert.equal(feedMark({ ...feed, siteUrl: "not a url" }).faviconUrl, null);
  const missing = feedMark(undefined, "播客 A");
  assert.equal(missing.letter, "播");
  assert.equal(missing.hue, siteHue("播客 A"));
  assert.equal(feedMark(undefined).letter, "?");
});
