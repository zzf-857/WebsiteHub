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
