import test from "node:test";
import assert from "node:assert/strict";

import {
  FeedsContractError,
  feedErrorLabel,
  formatDuration,
  groupItemsByFeed,
  normalizeFeed,
  normalizeFeedItem,
  normalizeFeedPicks,
  safeHref,
  type Feed,
  type FeedItem,
} from "../lib/feeds-contract.ts";

const FEED = {
  id: "f1",
  url: "https://blog.example/rss",
  title: "Blog",
  kind: "rss",
  site_url: "https://blog.example/",
  enabled: true,
  last_fetched_at: "2026-10-22T06:00:00Z",
  next_fetch_at: "2026-10-22T07:00:00Z",
  consecutive_failures: 0,
  last_error: null,
  item_count: 2,
};

const ITEM = {
  id: "i1",
  feed_id: "f1",
  url: "https://blog.example/1",
  title: "One",
  summary: "Body",
  author: null,
  published_at: "2026-10-22T05:00:00Z",
  audio_url: null,
  duration_seconds: null,
  fetched_at: "2026-10-22T06:00:00Z",
  read: false,
  dismissed: false,
  saved_site_id: null,
};

test("规范化订阅源与条目，snake_case 转为 camelCase", () => {
  const feed = normalizeFeed(FEED);
  assert.equal(feed.siteUrl, "https://blog.example/");
  assert.equal(feed.itemCount, 2);
  const item = normalizeFeedItem({ ...ITEM, duration_seconds: 3723, audio_url: "https://cdn.example/a.mp3" });
  assert.equal(item.durationSeconds, 3723);
  assert.equal(item.audioUrl, "https://cdn.example/a.mp3");
});

test("非 http(s) 链接一律丢弃，不会渲染成 href", () => {
  assert.equal(safeHref("javascript:alert(1)"), null);
  assert.equal(safeHref("data:text/html,x"), null);
  assert.equal(safeHref("not a url"), null);
  assert.equal(safeHref("https://ok.example/a"), "https://ok.example/a");
  assert.equal(normalizeFeedItem({ ...ITEM, url: "javascript:alert(1)" }).url, null);
});

test("字段类型不符时抛出契约错误", () => {
  assert.throws(() => normalizeFeed({ ...FEED, kind: "atom" }), FeedsContractError);
  assert.throws(() => normalizeFeedItem({ ...ITEM, read: "yes" }), FeedsContractError);
  assert.throws(() => normalizeFeedPicks({ items: [{ item: ITEM }] }), FeedsContractError);
});

test("精选带来源、理由与匹配标签", () => {
  const [pick] = normalizeFeedPicks({
    generated_at: "2026-10-22T06:00:00Z",
    items: [{ item: ITEM, feed_title: "Blog", feed_kind: "podcast", reason: "与你常收藏的「rust」相关", matched: ["rust"] }],
  });
  assert.equal(pick.feedKind, "podcast");
  assert.deepEqual(pick.matched, ["rust"]);
});

test("节目时长格式", () => {
  assert.equal(formatDuration(3723), "1:02:03");
  assert.equal(formatDuration(2700), "45:00");
  assert.equal(formatDuration(null), "");
  assert.equal(formatDuration(0), "");
});

test("错误码映射为中文说明，未知码有兜底", () => {
  assert.equal(feedErrorLabel("not_a_feed"), "不是 RSS / Atom 订阅地址");
  assert.equal(feedErrorLabel("weird"), "拉取失败");
  assert.equal(feedErrorLabel(null), "");
});

test("全部视图按订阅源分组、组内按时间倒序、跳过已忽略与空组", () => {
  const feeds: Feed[] = [
    normalizeFeed(FEED),
    normalizeFeed({ ...FEED, id: "f2", title: "Empty" }),
    normalizeFeed({ ...FEED, id: "f3", title: "Pod" }),
  ];
  const items: FeedItem[] = [
    normalizeFeedItem({ ...ITEM, id: "old", published_at: "2026-10-20T00:00:00Z" }),
    normalizeFeedItem({ ...ITEM, id: "new", published_at: "2026-10-22T00:00:00Z" }),
    normalizeFeedItem({ ...ITEM, id: "gone", dismissed: true }),
    normalizeFeedItem({ ...ITEM, id: "p1", feed_id: "f3", published_at: null }),
  ];
  const groups = groupItemsByFeed(feeds, items);
  assert.deepEqual(groups.map((group) => group.feed.id), ["f1", "f3"]);
  assert.deepEqual(groups[0].items.map((item) => item.id), ["new", "old"]);
});


test("订阅源与条目接受 image_url，并兼容旧响应缺失或空图片", () => {
  for (const [normalize, value] of [
    [normalizeFeed, FEED],
    [normalizeFeedItem, ITEM],
  ] as const) {
    assert.equal(normalize({ ...value, image_url: "https://cdn.example/cover.jpg" }).imageUrl,
      "https://cdn.example/cover.jpg");
    assert.equal(normalize(value).imageUrl, null);
    assert.equal(normalize({ ...value, image_url: null }).imageUrl, null);
    assert.equal(normalize({ ...value, image_url: "javascript:alert(1)" }).imageUrl, null);
  }
});

test("订阅源与条目拒绝错误类型的 image_url", () => {
  for (const value of [42, true, [], {}]) {
    assert.throws(() => normalizeFeed({ ...FEED, image_url: value }), FeedsContractError);
    assert.throws(() => normalizeFeedItem({ ...ITEM, image_url: value }), FeedsContractError);
  }
});
