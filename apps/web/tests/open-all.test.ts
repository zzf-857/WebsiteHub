import test from "node:test";
import assert from "node:assert/strict";

import {
  currentWindowNewTabTargets,
  currentWindowTarget,
  mergeAttempt,
  pendingTargets,
} from "../lib/open-all.ts";

const URLS = [
  "https://a.example.com",
  "https://b.example.com",
  "https://c.example.com",
  "https://d.example.com",
];

test("首次打开覆盖全部地址", () => {
  assert.deepEqual(pendingTargets(URLS, []), URLS);
});

test("重试只针对没打开的——这正是之前会重复开标签的地方", () => {
  // 第一轮：a、b 成功，c、d 被拦截。
  const opened = mergeAttempt([], URLS, ["https://c.example.com", "https://d.example.com"]);
  assert.deepEqual(opened, ["https://a.example.com", "https://b.example.com"]);

  // 第二轮只会尝试 c、d，a、b 不会被再开一遍。
  assert.deepEqual(pendingTargets(URLS, opened), [
    "https://c.example.com",
    "https://d.example.com",
  ]);
});

test("全部成功后没有剩余工作，调用方据此收起弹层", () => {
  const opened = mergeAttempt([], URLS, []);
  assert.deepEqual(pendingTargets(URLS, opened), []);
});

test("连续重试逐步收敛，累计结果不重复计数", () => {
  let opened = mergeAttempt([], URLS, ["https://b.example.com", "https://c.example.com", "https://d.example.com"]);
  opened = mergeAttempt(opened, pendingTargets(URLS, opened), ["https://d.example.com"]);
  assert.deepEqual(pendingTargets(URLS, opened), ["https://d.example.com"]);

  opened = mergeAttempt(opened, pendingTargets(URLS, opened), []);
  assert.deepEqual(pendingTargets(URLS, opened), []);
  // 每个地址只被记一次，即便多轮尝试里重复出现。
  assert.equal(new Set(opened).size, opened.length);
  assert.equal(opened.length, URLS.length);
});

test("当前窗口模式：第一个占用本页，其余开新标签", () => {
  assert.deepEqual(currentWindowNewTabTargets(URLS, []), URLS.slice(1));
  assert.equal(currentWindowTarget(URLS, []), "https://a.example.com");
});

test("当前窗口模式重试时，本页不再重复跳转", () => {
  // 第一轮：其余三个都开了，本页跳到了 a。
  const opened = mergeAttempt([], [...URLS.slice(1), URLS[0]], []);
  assert.equal(currentWindowTarget(URLS, opened), null);
  assert.deepEqual(currentWindowNewTabTargets(URLS, opened), []);
});

test("列表为空时没有可跳转目标，本页必须留在原地", () => {
  // 跳走会终止本页脚本，连重试机会一起没了。
  assert.equal(currentWindowTarget([], []), null);
  assert.deepEqual(currentWindowNewTabTargets([], []), []);
});
