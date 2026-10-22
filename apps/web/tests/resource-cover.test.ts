import assert from "node:assert/strict";
import test from "node:test";

import {
  CARD_CAPTION_HEIGHT,
  CARD_IMAGE_MIN_HEIGHT,
  CARD_REPO_HEIGHT,
  CARD_SITE_HEIGHT,
  cardOverlayText,
  coverModel,
  estimateCardHeight,
  estimateCoverHeight,
  formatStars,
  isSafePreviewUrl,
  repoCoordinates,
  repoStats,
  resourceKindLabel,
  resourceTitle,
  siteDomain,
  siteHue,
} from "../lib/resource-cover.ts";
import { siteFixture } from "./discover-fixtures.ts";

test("safe preview URLs are absolute http(s) without credentials", () => {
  assert.equal(isSafePreviewUrl("https://example.com/og.png"), true);
  assert.equal(isSafePreviewUrl("http://example.com/og.png"), true);
  assert.equal(isSafePreviewUrl("  https://example.com/og.png  "), true);
  assert.equal(isSafePreviewUrl("javascript:alert(1)"), false);
  assert.equal(isSafePreviewUrl("data:image/png;base64,AAAA"), false);
  assert.equal(isSafePreviewUrl("ftp://example.com/a.png"), false);
  assert.equal(isSafePreviewUrl("https://user:pass@example.com/a.png"), false);
  assert.equal(isSafePreviewUrl("/relative/path.png"), false);
  assert.equal(isSafePreviewUrl(""), false);
  assert.equal(isSafePreviewUrl(null), false);
  assert.equal(isSafePreviewUrl(`https://example.com/${"a".repeat(2100)}`), false);
});

test("a safe preview image wins and keeps the generated cover as its fallback", () => {
  const model = coverModel(
    siteFixture({ kind: "repo", originalUrl: "https://github.com/ollama/ollama", previewUrl: "https://opengraph.githubassets.com/1/ollama/ollama" }),
    "card",
  );
  assert.equal(model.type, "image");
  if (model.type !== "image") return;
  assert.equal(model.aspect, "1.91 / 1");
  assert.equal(model.src, "https://opengraph.githubassets.com/1/ollama/ollama");
  assert.equal(model.fallback.type, "repo");

  const video = coverModel(
    siteFixture({ kind: "video", originalUrl: "https://www.bilibili.com/video/BV1x", previewUrl: "https://i0.hdslb.com/cover.jpg" }),
    "card",
  );
  assert.equal(video.type, "image");
  if (video.type === "image") {
    assert.equal(video.aspect, "16 / 9");
    assert.equal(video.fallback.type, "video");
  }
});

test("an unsafe preview URL is ignored in favour of the generated cover", () => {
  const model = coverModel(
    siteFixture({ kind: "video", originalUrl: "https://youtu.be/abc", previewUrl: "javascript:alert(1)" }),
    "card",
  );
  assert.equal(model.type, "video");
  assert.equal(model.domain, "youtu.be");
});

test("repo covers read owner/repo from meta first, then from the URL", () => {
  const fromMeta = coverModel(
    siteFixture({
      kind: "repo",
      originalUrl: "https://github.com/some/other/tree/main",
      meta: { host: "github.com", owner: "browser-use", repo: "browser-use" },
    }),
    "hero",
  );
  assert.equal(fromMeta.type, "repo");
  if (fromMeta.type === "repo") {
    assert.equal(fromMeta.owner, "browser-use");
    assert.equal(fromMeta.repo, "browser-use");
    assert.equal(fromMeta.size, "hero");
    assert.equal(fromMeta.language, null);
    assert.equal(fromMeta.stars, null);
  }

  assert.deepEqual(
    repoCoordinates(siteFixture({ originalUrl: "https://github.com/excalidraw/excalidraw.git", meta: {} })),
    { host: "github.com", owner: "excalidraw", repo: "excalidraw" },
  );
  assert.deepEqual(
    repoCoordinates(siteFixture({ originalUrl: "https://huggingface.co/datasets/openai/gsm8k", meta: {} })),
    { host: "huggingface.co", owner: "openai", repo: "gsm8k" },
  );
  assert.equal(repoCoordinates(siteFixture({ originalUrl: "https://github.com/", meta: {} })), null);

  // 解析不出 owner/repo 的仓库退回站点封面，而不是画一个空仓库名
  assert.equal(coverModel(siteFixture({ kind: "repo", originalUrl: "https://github.com/" }), "card").type, "site");
});

test("repo stats come from Q45's meta.github and are formatted", () => {
  assert.deepEqual(repoStats({ github: { language: "Python", stars: 52_310 } }), { language: "Python", stars: "5.2 万" });
  assert.deepEqual(repoStats({ language: "Go", stars: 980 }), { language: "Go", stars: "980" });
  assert.deepEqual(repoStats({ github: { language: " ", stars: "many" } }), { language: null, stars: null });
  assert.equal(formatStars(1_000), "1k");
  assert.equal(formatStars(1_234), "1.2k");
  assert.equal(formatStars(10_000), "1 万");
  assert.equal(formatStars(34_567), "3.4 万");
  assert.equal(formatStars(-3), "0");
});

test("article covers need a summary; without one they fall back to the site cover", () => {
  const withSummary = coverModel(
    siteFixture({ kind: "article", summary: "先拍够曝光，再在后期合成，比赌一张更稳妥可靠。" }),
    "card",
  );
  assert.equal(withSummary.type, "article");
  if (withSummary.type === "article") assert.equal(withSummary.hook, "先拍够曝光，再在后期合成，比赌一张更稳妥可靠。");

  const withoutSummary = coverModel(
    siteFixture({ kind: "article", summary: "   ", description: "一段很长的网页描述" }),
    "card",
  );
  assert.equal(withoutSummary.type, "site");
});

test("the hover overlay never repeats the hook already drawn on an article cover", () => {
  const hook = "先拍够曝光，再在后期合成，比赌一张更稳妥可靠。";
  assert.equal(cardOverlayText(siteFixture({ kind: "article", summary: hook })), "");
  assert.equal(
    cardOverlayText(siteFixture({ kind: "article", summary: hook, description: "包围曝光的完整流程与参数。" })),
    "包围曝光的完整流程与参数。",
  );
  assert.equal(cardOverlayText(siteFixture({ kind: "article", summary: hook, description: hook })), "");
  // 有预览图时封面不画钩子，遮罩照常给简介
  assert.equal(
    cardOverlayText(siteFixture({ kind: "article", summary: hook, previewUrl: "https://example.com/og.png" })),
    hook,
  );
  // 其他类型：简介优先，缺失时退回详细介绍
  assert.equal(cardOverlayText(siteFixture({ summary: null, description: "网址检测工具" })), "网址检测工具");
  assert.equal(cardOverlayText(siteFixture({ summary: "多引擎检测文件和网址的安全性" })), "多引擎检测文件和网址的安全性");
});

test("websites and unknown shapes use the site cover with the real favicon", () => {
  const model = coverModel(siteFixture({ faviconUrl: "https://www.virustotal.com/favicon.ico" }), "thumb");
  assert.equal(model.type, "site");
  if (model.type === "site") {
    assert.equal(model.domain, "virustotal.com");
    assert.equal(model.faviconUrl, "https://www.virustotal.com/favicon.ico");
    assert.equal(model.size, "thumb");
  }
});

test("site hue is deterministic, bounded and spreads across domains", () => {
  assert.equal(siteHue("linux.do"), siteHue("linux.do"));
  assert.equal(siteHue("LINUX.DO"), siteHue("linux.do"));
  const hues = ["github.com", "linux.do", "v2ex.com", "lmstudio.ai", "virustotal.com", "bilibili.com"].map(siteHue);
  for (const hue of hues) {
    assert.ok(Number.isInteger(hue) && hue >= 0 && hue < 360);
  }
  assert.ok(new Set(hues).size >= 5, "不同域名应当得到不同色相");
  const model = coverModel(siteFixture({ originalUrl: "https://www.linux.do/t/123" }), "card");
  assert.equal(model.hue, siteHue("linux.do"));
});

test("site domain strips www and survives malformed input", () => {
  assert.equal(siteDomain("https://WWW.Example.com./path"), "example.com");
  assert.equal(siteDomain("not a url"), "not a url");
});

test("card height estimates follow the cover type plus a fixed caption", () => {
  assert.equal(estimateCoverHeight(coverModel(siteFixture({ kind: "repo", originalUrl: "https://github.com/a/b" }), "card"), 300), CARD_REPO_HEIGHT);
  assert.equal(estimateCoverHeight(coverModel(siteFixture(), "card"), 300), CARD_SITE_HEIGHT);
  assert.equal(
    estimateCoverHeight(coverModel(siteFixture({ previewUrl: "https://example.com/og.png" }), "card"), 300),
    Math.max(CARD_IMAGE_MIN_HEIGHT, Math.round(300 / 1.91)),
  );
  assert.equal(
    estimateCoverHeight(coverModel(siteFixture({ previewUrl: "https://example.com/og.png" }), "card"), 600),
    Math.round(600 / 1.91),
  );
  const shortHook = estimateCoverHeight(coverModel(siteFixture({ kind: "article", summary: "短要点，二十个字以内也能撑起最小高度。" }), "card"), 300);
  const longHook = estimateCoverHeight(
    coverModel(siteFixture({ kind: "article", summary: "光比超出宽容度时，多拍几张再合成，比赌一张更稳，阴影与高光都能保住细节。" }), "card"),
    220,
  );
  assert.ok(shortHook >= 200);
  assert.ok(longHook > shortHook);
  assert.equal(estimateCardHeight(siteFixture(), 300), CARD_SITE_HEIGHT + CARD_CAPTION_HEIGHT);
});

test("titles stay readable when the saved name is only a host or URL", () => {
  assert.equal(resourceTitle(siteFixture({ name: "GitHub - ollama/ollama: Get up and running" })), "ollama/ollama: Get up and running");
  assert.equal(
    resourceTitle(siteFixture({ kind: "repo", name: "github.com", originalUrl: "https://github.com/localsend/localsend" })),
    "localsend/localsend",
  );
  assert.equal(
    resourceTitle(siteFixture({ kind: "article", name: "https://linux.do/t/topic/42", originalUrl: "https://linux.do/t/topic/42" })),
    "linux.do/t/topic/42",
  );
  assert.equal(resourceTitle(siteFixture({ name: "LM Studio", originalUrl: "https://lmstudio.ai/" })), "LM Studio");
});

test("kind labels name the code host for repositories", () => {
  assert.equal(resourceKindLabel(siteFixture({ kind: "repo", originalUrl: "https://github.com/a/b" })), "GitHub 仓库");
  assert.equal(resourceKindLabel(siteFixture({ kind: "repo", originalUrl: "https://git.example.com/a/b" })), "代码仓库");
  assert.equal(resourceKindLabel(siteFixture({ kind: "article" })), "帖子与文章");
  assert.equal(resourceKindLabel(siteFixture({ kind: "video" })), "视频");
  assert.equal(resourceKindLabel(siteFixture()), "工具与网站");
});
