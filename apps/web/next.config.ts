import type { NextConfig } from "next";

const configuredDevOrigins = process.env.WEBHUB_ALLOWED_DEV_ORIGINS
  ?.split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

const apiBaseUrl = (process.env.WEBHUB_API_INTERNAL_URL ?? "http://127.0.0.1:8100")
  .replace(/\/+$/, "");

const EXTERNAL_REWRITE_PROXY_TIMEOUT_MS = 45 * 60 * 1000;

const nextConfig: NextConfig = {
  allowedDevOrigins: configuredDevOrigins ?? ["localhost", "127.0.0.1"],
  poweredByHeader: false,
  experimental: {
    // Worst legal plan: ceil(50 batches / 4 concurrent) * 2 attempts * 90 seconds = 39 minutes.
    // This remains a synchronous flow, so the rewrite must outlive that backend-owned hard bound.
    proxyTimeout: EXTERNAL_REWRITE_PROXY_TIMEOUT_MS,
  },
  // Q44 前站内对话嵌在首页，地址栏里的会话链接一直是 /?c=<id>，“让 Agent 帮你”
  // 预填是 /?ask=；书签与历史记录里还留着这些地址。首页改成发现页后转到 /chat，
  // Next 的 redirect 默认原样带上查询参数，面板挂载时照旧读 ?c= / ?ask=。
  async redirects() {
    return ["c", "ask"].map((key) => ({
      source: "/",
      has: [{ type: "query" as const, key }],
      destination: "/chat",
      permanent: false,
    }));
  },
  async rewrites() {
    return [
      {
        source: "/api/backend/:path*",
        destination: `${apiBaseUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
