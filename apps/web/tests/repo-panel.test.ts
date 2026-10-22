import assert from "node:assert/strict";
import test from "node:test";

import {
  pushedAgoLabel,
  REPO_NOT_FOUND_NOTICE,
  REPO_RATE_LIMITED_NOTICE,
  repoPanelModel,
} from "../lib/repo-panel.ts";

const NOW = Date.parse("2026-10-22T08:00:00Z");

test("Q45: pushed label counts days, then months and years", () => {
  assert.equal(pushedAgoLabel("2026-10-22T01:00:00Z", NOW), "今天推送");
  assert.equal(pushedAgoLabel("2026-10-20T07:00:00Z", NOW), "2 天前推送");
  assert.equal(pushedAgoLabel("2026-09-23T08:00:00Z", NOW), "29 天前推送");
  assert.equal(pushedAgoLabel("2026-08-01T00:00:00Z", NOW), "2 个月前推送");
  assert.equal(pushedAgoLabel("2023-10-01T00:00:00Z", NOW), "3 年前推送");
  // 时钟偏差导致的未来时间按今天算；无法解析时不显示。
  assert.equal(pushedAgoLabel("2026-10-23T08:00:00Z", NOW), "今天推送");
  assert.equal(pushedAgoLabel("not a date", NOW), null);
});

test("Q45: repo panel shows stars, forks, language, topics, license, push age and archive state", () => {
  const model = repoPanelModel(
    {
      host: "github.com",
      owner: "langchain-ai",
      repo: "langgraph",
      github: {
        stars: 52_310,
        forks: 9_012,
        language: "Python",
        topics: ["agents", "llm"],
        license: "MIT",
        pushed_at: "2026-10-20T07:00:00Z",
        archived: true,
        fetched_at: "2026-10-22T07:00:00Z",
        etag: '"v1"',
      },
    },
    NOW,
  );
  assert.deepEqual(model, {
    stars: "5.2 万",
    forks: "9k",
    language: "Python",
    topics: ["agents", "llm"],
    license: "MIT",
    pushed: "2 天前推送",
    archived: true,
    notice: null,
  });
});

test("Q45: repo panel explains missing numbers and hides when there is nothing to say", () => {
  assert.equal(repoPanelModel({ host: "github.com", owner: "a", repo: "b" }, NOW), null);
  assert.equal(repoPanelModel({ github: { fetched_at: "2026-10-22T07:00:00Z" } }, NOW), null);

  const missing = repoPanelModel({ github: { not_found: true, fetched_at: "2026-10-22T07:00:00Z" } }, NOW);
  assert.equal(missing?.notice, REPO_NOT_FOUND_NOTICE);
  assert.equal(missing?.stars, null);

  const limited = repoPanelModel({ github: { rate_limited_at: "2026-10-22T07:00:00Z" } }, NOW);
  assert.equal(limited?.notice, REPO_RATE_LIMITED_NOTICE);

  // 被限流但保留了上次的数据：照常显示数字，不再提示。
  const kept = repoPanelModel(
    { github: { stars: 999, forks: 0, rate_limited_at: "2026-10-22T07:00:00Z" } },
    NOW,
  );
  assert.equal(kept?.notice, null);
  assert.equal(kept?.stars, "999");
  assert.equal(kept?.forks, "0");
  assert.equal(kept?.pushed, null);
});
