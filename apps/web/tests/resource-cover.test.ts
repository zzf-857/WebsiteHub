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
  githubRepoMeta,
  heroSummaryText,
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

test("repo covers only carry a bottom row when meta has language or stars", () => {
  // 顶行已写托管站点与 owner；没有统计数据时 language / stars 都为空，底行整行不画
  for (const size of ["card", "hero"] as const) {
    const bare = coverModel(siteFixture({ kind: "repo", originalUrl: "https://github.com/ollama/ollama" }), size);
    assert.equal(bare.type, "repo");
    if (bare.type === "repo") {
      assert.equal(bare.host, "github.com");
      assert.equal(bare.language, null);
      assert.equal(bare.stars, null);
    }
  }

  const languageOnly = coverModel(
    siteFixture({ kind: "repo", originalUrl: "https://github.com/ollama/ollama", meta: { github: { language: "Go" } } }),
    "hero",
  );
  assert.equal(languageOnly.type, "repo");
  if (languageOnly.type === "repo") {
    assert.equal(languageOnly.language, "Go");
    assert.equal(languageOnly.stars, null);
  }

  const starsOnly = coverModel(
    siteFixture({ kind: "repo", originalUrl: "https://github.com/ollama/ollama", meta: { github: { stars: 1_234 } } }),
    "card",
  );
  assert.equal(starsOnly.type, "repo");
  if (starsOnly.type === "repo") {
    assert.equal(starsOnly.language, null);
    assert.equal(starsOnly.stars, "1.2k");
  }
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

test("the hero copy shows the summary unless the cover already draws it", () => {
  // 站点 / 图片封面：简介优先，缺失时退回详细介绍
  assert.equal(heroSummaryText(siteFixture({ summary: "多引擎检测文件和网址的安全性", description: "网页描述" })), "多引擎检测文件和网址的安全性");
  assert.equal(heroSummaryText(siteFixture({ summary: "  ", description: "网址检测工具" })), "网址检测工具");
  assert.equal(heroSummaryText(siteFixture({ summary: null, description: null })), "");

  // 要点封面画了钩子，仓库主卡封面画了一句话介绍：只在详细介绍另有内容时才写
  const hook = "先拍够曝光，再在后期合成，比赌一张更稳妥可靠。";
  assert.equal(heroSummaryText(siteFixture({ kind: "article", summary: hook })), "");
  assert.equal(
    heroSummaryText(siteFixture({ kind: "article", summary: hook, description: "包围曝光的完整流程与参数。" })),
    "包围曝光的完整流程与参数。",
  );
  const repo = { kind: "repo" as const, originalUrl: "https://github.com/ollama/ollama" };
  assert.equal(heroSummaryText(siteFixture({ ...repo, summary: "本地运行大模型" })), "");
  assert.equal(heroSummaryText(siteFixture({ ...repo, summary: null, description: "本地运行大模型" })), "");
  assert.equal(
    heroSummaryText(siteFixture({ ...repo, summary: "本地运行大模型", description: "支持 Llama、Qwen 等模型。" })),
    "支持 Llama、Qwen 等模型。",
  );
  // 有预览图时封面不画文字，照常给简介
  assert.equal(
    heroSummaryText(siteFixture({ ...repo, summary: "本地运行大模型", previewUrl: "https://example.com/og.png" })),
    "本地运行大模型",
  );
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

test("Q45: star counts format to k / 万 / 亿 without rounding up", () => {
  assert.equal(formatStars(999), "999");
  assert.equal(formatStars(9_999), "9.9k");
  assert.equal(formatStars(12_000), "1.2 万");
  assert.equal(formatStars(1_234_567), "123.4 万");
  assert.equal(formatStars(99_999_999), "9999.9 万");
  assert.equal(formatStars(100_000_000), "1 亿");
  assert.equal(formatStars(123_456_789), "1.2 亿");
  assert.equal(formatStars(12.9), "12");
  assert.equal(formatStars(Number.NaN), "0");
});

test("Q45: meta.github parses into a typed record for covers and the detail page", () => {
  assert.equal(githubRepoMeta({}), null);
  assert.equal(githubRepoMeta({ github: "broken" }), null);
  assert.deepEqual(
    githubRepoMeta({
      host: "github.com",
      owner: "langchain-ai",
      repo: "langgraph",
      github: {
        stars: 52_310,
        forks: 9_000,
        language: "Python",
        topics: ["agents", 3, "", "llm"],
        license: "MIT",
        pushed_at: "2026-10-20T00:00:00Z",
        archived: true,
        description: "Build agents as graphs.",
        homepage: "javascript:alert(1)",
        fetched_at: "2026-10-22T08:00:00Z",
        etag: '"abc"',
      },
    }),
    {
      stars: 52_310,
      forks: 9_000,
      language: "Python",
      topics: ["agents", "llm"],
      license: "MIT",
      pushedAt: "2026-10-20T00:00:00Z",
      archived: true,
      description: "Build agents as graphs.",
      homepage: null,
      fetchedAt: "2026-10-22T08:00:00Z",
      notFound: false,
      rateLimitedAt: null,
    },
  );
  const missing = githubRepoMeta({ github: { not_found: true, fetched_at: "2026-10-22T08:00:00Z" } });
  assert.equal(missing?.notFound, true);
  assert.equal(missing?.stars, null);
  assert.deepEqual(repoStats({ github: { not_found: true } }), { language: null, stars: null });
  const limited = githubRepoMeta({ github: { stars: 10, rate_limited_at: "2026-10-22T09:00:00Z" } });
  assert.equal(limited?.rateLimitedAt, "2026-10-22T09:00:00Z");
  assert.deepEqual(repoStats({ github: { stars: 10, rate_limited_at: "x" } }), { language: null, stars: "10" });
});

test("Q45: GitHub's auto-generated social card yields to the repo cover once stats exist", () => {
  const autoCard = "https://opengraph.githubassets.com/1/langchain-ai/langgraph";
  const withStats = coverModel(
    siteFixture({
      kind: "repo",
      originalUrl: "https://github.com/langchain-ai/langgraph",
      previewUrl: autoCard,
      meta: { host: "github.com", owner: "langchain-ai", repo: "langgraph", github: { stars: 52_310, language: "Python" } },
    }),
    "card",
  );
  assert.equal(withStats.type, "repo");
  if (withStats.type === "repo") {
    assert.equal(withStats.stars, "5.2 万");
    assert.equal(withStats.language, "Python");
  }

  // Without numbers the GitHub card still carries more than the generated one.
  const withoutStats = coverModel(
    siteFixture({
      kind: "repo",
      originalUrl: "https://github.com/langchain-ai/langgraph",
      previewUrl: autoCard,
      meta: { host: "github.com", owner: "langchain-ai", repo: "langgraph", github: { rate_limited_at: "x" } },
    }),
    "card",
  );
  assert.equal(withoutStats.type, "image");

  // A preview the author uploaded is real artwork and keeps priority.
  const customPreview = coverModel(
    siteFixture({
      kind: "repo",
      originalUrl: "https://github.com/langchain-ai/langgraph",
      previewUrl: "https://repository-images.githubusercontent.com/1/abc",
      meta: { host: "github.com", owner: "langchain-ai", repo: "langgraph", github: { stars: 1 } },
    }),
    "card",
  );
  assert.equal(customPreview.type, "image");
});
