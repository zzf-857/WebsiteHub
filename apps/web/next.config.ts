import type { NextConfig } from "next";

const configuredDevOrigins = process.env.WEBHUB_ALLOWED_DEV_ORIGINS
  ?.split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

const apiBaseUrl = (process.env.WEBHUB_API_INTERNAL_URL ?? "http://127.0.0.1:8100")
  .replace(/\/+$/, "");

const nextConfig: NextConfig = {
  allowedDevOrigins: configuredDevOrigins ?? ["localhost", "127.0.0.1"],
  poweredByHeader: false,
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
