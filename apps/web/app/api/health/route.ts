import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const API_BASE_URL =
  process.env.WEBHUB_API_INTERNAL_URL ?? "http://127.0.0.1:8100";

export async function GET() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3_500);
  const startedAt = performance.now();

  try {
    const endpoint = new URL("/api/health", `${API_BASE_URL.replace(/\/+$/, "")}/`);
    const response = await fetch(endpoint, {
      cache: "no-store",
      headers: { accept: "application/json" },
      signal: controller.signal,
    });

    if (!response.ok) {
      return NextResponse.json(
        {
          status: "unavailable",
          service: "webhub-api",
          upstreamStatus: response.status,
        },
        { status: 503 },
      );
    }

    const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;

    return NextResponse.json({
      ...payload,
      status: "ok",
      service: typeof payload.service === "string" ? payload.service : "webhub-api",
      latencyMs: Math.round(performance.now() - startedAt),
    });
  } catch {
    return NextResponse.json(
      { status: "unavailable", service: "webhub-api" },
      { status: 503 },
    );
  } finally {
    clearTimeout(timeout);
  }
}
