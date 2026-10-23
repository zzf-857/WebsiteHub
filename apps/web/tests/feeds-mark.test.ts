import assert from "node:assert/strict";
import test from "node:test";

import { feedMark, feedMarkSource } from "../lib/feeds-mark.ts";
import { proxiedImageUrl } from "../lib/media-proxy.ts";
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


test("feedMarkSource: feed image precedes favicon and letter, including load failures", () => {
  const imageUrl = "https://cdn.example/cover.jpg";
  const mark = feedMark({ ...feed, imageUrl });
  const image = proxiedImageUrl(imageUrl)!;
  const favicon = proxiedImageUrl(mark.faviconUrl)!;
  assert.deepEqual(feedMarkSource(mark, false), { kind: "image", url: image });
  assert.deepEqual(feedMarkSource(mark, false, new Set([image])), { kind: "favicon", url: favicon });
  assert.deepEqual(feedMarkSource(mark, false, new Set([image, favicon])), { kind: "letter" });
  assert.deepEqual(feedMarkSource(feedMark(feed), false), { kind: "favicon", url: favicon });
  assert.deepEqual(feedMarkSource(feedMark({ ...feed, siteUrl: null }), false), { kind: "letter" });
});

test("feedMarkSource: podcast uses feed cover or headphones, never a favicon", () => {
  const mark = feedMark({ ...feed, imageUrl: "https://cdn.example/podcast.jpg" });
  const image = proxiedImageUrl(mark.imageUrl)!;
  assert.deepEqual(feedMarkSource(mark, true), { kind: "image", url: image });
  assert.deepEqual(feedMarkSource(mark, true, new Set([image])), { kind: "podcast" });
  assert.deepEqual(feedMarkSource(feedMark(feed), true), { kind: "podcast" });
});

test("feedMarkSource: unproxyable feed images continue to the next safe source", () => {
  for (const imageUrl of ["data:image/png,x", "https://user:pass@example.com/x", "bad"]) {
    const source = feedMarkSource(feedMark({ ...feed, imageUrl }), false);
    assert.deepEqual(source, { kind: "favicon", url: proxiedImageUrl("https://sspai.com/favicon.ico") });
  }
});
