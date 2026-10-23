import assert from "node:assert/strict";
import test from "node:test";

import {
  applyPortabilityImport,
  PORTABILITY_EXPORT_URL,
  PortabilityApiError,
  uploadPortabilityBackup,
} from "../lib/portability-client.ts";

const preview = {
  id: "preview-1", status: "previewed", expires_at: "2026-10-24T08:00:00Z", payload_sha256: "a".repeat(64),
  counts: { categories: {}, tags: {}, sites: { create: 1 }, spaces: {}, feeds: {}, conversations: {} },
};
const result = { id: "preview-1", status: "applied", applied_at: "2026-10-23T09:00:00Z", created: { sites: 1 }, merged: {} };
const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

test("uploads backup bytes directly with same-origin cookies and an abort signal", async (context) => {
  const file = new Blob(['{"format":"webhub-backup"}'], { type: "application/json" });
  const controller = new AbortController();
  context.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    assert.equal(url, "/api/backend/portability/imports");
    assert.equal(init.method, "POST");
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.body, file);
    assert.deepEqual(init.headers, { "Content-Type": "application/json" });
    assert.equal(init.signal, controller.signal);
    return jsonResponse(preview, 201);
  });
  assert.equal((await uploadPortabilityBackup(file, controller.signal)).counts.sites.create, 1);
  assert.equal(PORTABILITY_EXPORT_URL, "/api/backend/portability/export");
});

test("applies the exact preview SHA with encoded identifier and returns normalized result", async (context) => {
  context.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    assert.equal(url, "/api/backend/portability/imports/id%2Fwith%20space/apply");
    assert.equal(init.method, "POST");
    assert.equal(init.credentials, "same-origin");
    assert.deepEqual(JSON.parse(String(init.body)), { expected_sha256: "a".repeat(64) });
    return jsonResponse(result);
  });
  assert.equal((await applyPortabilityImport("id/with space", "a".repeat(64))).created.sites, 1);
});

test("maps every backup domain error and retains safe validation paths", async (context) => {
  for (const [code, status] of [
    ["payload_too_large", 413], ["invalid_format", 422], ["unsupported_version", 422],
    ["limits_exceeded", 422], ["preview_expired", 409], ["checksum_mismatch", 409], ["not_found", 404],
  ] as const) {
    const mock = context.mock.method(globalThis, "fetch", async () => jsonResponse({ detail: { code, message: "备份校验失败：sites.0.url，请检查文件后重试。" } }, status));
    await assert.rejects(uploadPortabilityBackup(new Blob(["{}"])), (error: unknown) => {
      assert.ok(error instanceof PortabilityApiError);
      assert.equal(error.code, code);
      assert.equal(error.status, status);
      assert.match(error.message, /sites\.0\.url/u);
      return true;
    });
    mock.mock.restore();
  }
});

test("maps plain proxy 413 and never exposes unknown upstream text", async (context) => {
  let response = new Response("internal upstream details", { status: 413 });
  context.mock.method(globalThis, "fetch", async () => response);
  await assert.rejects(uploadPortabilityBackup(new Blob(["{}"])), (error: unknown) => {
    assert.ok(error instanceof PortabilityApiError);
    assert.equal(error.code, "payload_too_large");
    assert.match(error.message, /64 MiB/u);
    return true;
  });
  response = jsonResponse({ detail: { code: "unrecognized", message: "sensitive upstream diagnostic" } }, 500);
  await assert.rejects(applyPortabilityImport("id", "a".repeat(64)), (error: unknown) => {
    assert.ok(error instanceof PortabilityApiError);
    assert.equal(error.message, "备份操作未完成，请重试。");
    return true;
  });
});

test("provides actionable messages for expired sessions, invalid Origin and malformed errors", async (context) => {
  let response = jsonResponse({}, 401);
  context.mock.method(globalThis, "fetch", async () => response);
  await assert.rejects(uploadPortabilityBackup(new Blob(["{}"])), /重新登录/u);
  response = jsonResponse({}, 403);
  await assert.rejects(uploadPortabilityBackup(new Blob(["{}"])), /刷新页面/u);
  response = new Response("{", { status: 502, headers: { "Content-Type": "application/json" } });
  await assert.rejects(uploadPortabilityBackup(new Blob(["{}"])), /请重试/u);
});

test("maps network failures to Chinese while preserving explicit cancellation", async (context) => {
  const original = new TypeError("Failed to fetch");
  context.mock.method(globalThis, "fetch", async () => { throw original; });
  await assert.rejects(uploadPortabilityBackup(new Blob(["{}"])), /网络连接中断/u);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(uploadPortabilityBackup(new Blob(["{}"]), controller.signal), (error: unknown) => error === original);
});
