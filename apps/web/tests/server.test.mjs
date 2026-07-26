import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer, request as httpRequest } from "node:http";
import { Readable } from "node:stream";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildBookmarkUploadTarget,
  buildAllowedHosts,
  createBookmarkUploadProxy,
  isDevelopmentMode,
  isAllowedHost,
  isBookmarkUploadRequest,
  normalizeHostHeader,
  normalizeRemoteAddress,
  overwriteForwardingHeaders,
  prepareNextApplication,
} from "../server.mjs";

const MOCK_EXPORT = fileURLToPath(
  new URL("../../../MockData/bookmarks_2026_7_26.html", import.meta.url),
);
const MOCK_EXPORT_SIZE = 1_601_123;
const MOCK_EXPORT_SHA256 = "c3dc4d28a504d2974a16a1aea7053fdecf83e1871245d68dc9a46583346c2785";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function listen(server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert(address && typeof address === "object");
  return `http://127.0.0.1:${address.port}`;
}

async function closeServer(server) {
  if (!server.listening) return;
  const closed = once(server, "close");
  server.close();
  server.closeAllConnections();
  await closed;
}

function createProxyServer({ target, maxBytes, idleTimeoutMs, fallback }) {
  const proxyBookmarkUpload = createBookmarkUploadProxy({
    target: buildBookmarkUploadTarget(target),
    maxBytes,
    idleTimeoutMs,
  });
  return createServer((request, response) => {
    overwriteForwardingHeaders(request.headers, request.socket);
    if (proxyBookmarkUpload(request, response)) return;
    if (fallback) {
      fallback(request, response);
      return;
    }
    response.statusCode = 404;
    response.end("Not Found");
  });
}

function sendRequest({ baseUrl, path, method = "POST", headers = {}, body }) {
  return new Promise((resolve, reject) => {
    const request = httpRequest(new URL(path, baseUrl), { method, headers });
    let responseStarted = false;
    request.once("response", async (response) => {
      responseStarted = true;
      try {
        const chunks = [];
        for await (const chunk of response) chunks.push(chunk);
        resolve({
          statusCode: response.statusCode,
          headers: response.headers,
          body: Buffer.concat(chunks),
        });
      } catch (error) {
        reject(error);
      }
    });
    request.once("error", (error) => {
      if (!responseStarted) reject(error);
    });

    if (body && typeof body.pipe === "function") {
      body.once("error", reject);
      body.pipe(request);
    } else {
      request.end(body);
    }
  });
}

async function inspectRequest(request) {
  const digest = createHash("sha256");
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    digest.update(chunk);
  }
  return {
    method: request.method,
    url: request.url,
    headers: { ...request.headers },
    size,
    sha256: digest.digest("hex"),
  };
}

test("uses development mode only when the dev flag is explicit", () => {
  assert.equal(isDevelopmentMode(["node", "server.mjs", "--dev"]), true);
  assert.equal(isDevelopmentMode(["node", "server.mjs"]), false);
});

test("normalizes IPv4-mapped socket addresses to one IP", () => {
  assert.equal(normalizeRemoteAddress("::ffff:192.168.0.25"), "192.168.0.25");
  assert.equal(normalizeRemoteAddress("fe80::1%12"), "fe80::1");
  assert.equal(normalizeRemoteAddress("attacker, 127.0.0.1"), null);
});

test("allows only local or explicitly configured request hosts", () => {
  const allowedHosts = buildAllowedHosts({
    configured: "webhub.home, extra.example:8443",
    hostname: "DESKTOP-WEBHUB",
    interfaces: {
      Ethernet: [{ address: "192.168.0.100" }],
      Loopback: [{ address: "::1" }],
    },
  });

  assert.equal(normalizeHostHeader("LOCALHOST:3100"), "localhost");
  assert.equal(isAllowedHost("192.168.0.100:3100", allowedHosts), true);
  assert.equal(isAllowedHost("desktop-webhub:3100", allowedHosts), true);
  assert.equal(isAllowedHost("webhub.home", allowedHosts), true);
  assert.equal(isAllowedHost("extra.example:8443", allowedHosts), true);
  assert.equal(isAllowedHost("attacker.invalid", allowedHosts), false);
  assert.equal(isAllowedHost("localhost@attacker.invalid", allowedHosts), false);
  assert.equal(isAllowedHost("localhost:3100/path", allowedHosts), false);
  assert.equal(isAllowedHost("localhost :3100", allowedHosts), false);
});

test("builds the host allowlist after Next loads its runtime environment", async () => {
  const originalAllowedHosts = process.env.WEBHUB_ALLOWED_HOSTS;
  const lifecycle = [];

  try {
    delete process.env.WEBHUB_ALLOWED_HOSTS;
    const { allowedHosts } = await prepareNextApplication({
      dev: true,
      hostname: "127.0.0.1",
      port: 3100,
      createApplication(options) {
        lifecycle.push(["create", options]);
        return {
          async prepare() {
            lifecycle.push(["prepare"]);
            process.env.WEBHUB_ALLOWED_HOSTS = "env-loaded.webhub";
          },
        };
      },
    });

    assert.deepEqual(lifecycle, [
      ["create", { dev: true, hostname: "127.0.0.1", port: 3100 }],
      ["prepare"],
    ]);
    assert.equal(allowedHosts.has("env-loaded.webhub"), true);
  } finally {
    if (originalAllowedHosts === undefined) delete process.env.WEBHUB_ALLOWED_HOSTS;
    else process.env.WEBHUB_ALLOWED_HOSTS = originalAllowedHosts;
  }
});

test("overwrites forged forwarding headers with the direct socket hop", () => {
  const headers = {
    host: "192.168.0.100:3100",
    forwarded: "for=203.0.113.9;host=attacker.invalid;proto=https",
    "x-real-ip": "203.0.113.9",
    "x-forwarded-for": "203.0.113.9, 127.0.0.1",
    "x-forwarded-host": "attacker.invalid",
    "x-forwarded-proto": "https",
    "x-forwarded-port": "443",
  };

  overwriteForwardingHeaders(headers, {
    remoteAddress: "::ffff:192.168.0.25",
    localPort: 3100,
    encrypted: false,
  });

  assert.equal(headers["x-forwarded-for"], "192.168.0.25");
  assert.equal(headers["x-forwarded-host"], "192.168.0.100:3100");
  assert.equal(headers["x-forwarded-proto"], "http");
  assert.equal(headers["x-forwarded-port"], "3100");
  assert.equal("forwarded" in headers, false);
  assert.equal("x-real-ip" in headers, false);
});

test("matches only the bookmark upload POST pathname", () => {
  assert.equal(
    isBookmarkUploadRequest({ method: "POST", url: "/api/backend/bookmark-imports" }),
    true,
  );
  assert.equal(
    isBookmarkUploadRequest({
      method: "POST",
      url: "/api/backend/bookmark-imports?source=browser",
    }),
    true,
  );
  assert.equal(
    isBookmarkUploadRequest({ method: "GET", url: "/api/backend/bookmark-imports" }),
    false,
  );
  assert.equal(
    isBookmarkUploadRequest({ method: "POST", url: "/api/backend/bookmark-imports/" }),
    false,
  );
  assert.equal(
    isBookmarkUploadRequest({ method: "POST", url: "/api/backend/bookmark-imports/next" }),
    false,
  );
  assert.equal(
    isBookmarkUploadRequest({
      method: "POST",
      url: "http://attacker.invalid/api/backend/bookmark-imports",
    }),
    false,
  );
});

test("streams the real bookmark mock with exact bytes, headers, and response metadata", {
  timeout: 15_000,
}, async (context) => {
  let mockStats;
  try {
    mockStats = await stat(MOCK_EXPORT);
  } catch (error) {
    if (error?.code === "ENOENT") {
      context.skip("private bookmark mock is not available in this checkout");
      return;
    }
    throw error;
  }

  const captured = deferred();
  const upstream = createServer((request, response) => {
    void inspectRequest(request).then((inspection) => {
      captured.resolve(inspection);
      const payload = Buffer.from('{"job_id":"job-real-mock"}');
      response.writeHead(201, {
        "Content-Type": "application/json",
        "Content-Length": String(payload.length),
        "Location": "/api/bookmark-imports/job-real-mock",
        "Set-Cookie": [
          "first_cookie=one; Path=/; HttpOnly",
          "second_cookie=two; Path=/; SameSite=Lax",
        ],
        "Connection": "keep-alive, x-upstream-hop",
        "X-Upstream-Hop": "remove-me",
      });
      response.end(payload);
    }, captured.reject);
  });
  const upstreamBaseUrl = await listen(upstream);
  const gateway = createProxyServer({ target: upstreamBaseUrl });
  const gatewayBaseUrl = await listen(gateway);
  context.after(() => closeServer(gateway));
  context.after(() => closeServer(upstream));

  assert.equal(mockStats.size, MOCK_EXPORT_SIZE);
  const response = await sendRequest({
    baseUrl: gatewayBaseUrl,
    path: "/api/backend/bookmark-imports?source=browser",
    headers: {
      "Content-Type": "application/octet-stream",
      "Content-Length": String(mockStats.size),
      "Cookie": "webhub_session=session-token",
      "Origin": gatewayBaseUrl,
      "Idempotency-Key": "upload-real-mock-request-0001",
      "X-Bookmark-Filename": "bookmarks_2026_7_26.html",
      "Forwarded": "for=203.0.113.8;host=attacker.invalid;proto=https",
      "X-Forwarded-For": "203.0.113.8",
      "X-Forwarded-Host": "attacker.invalid",
      "X-Real-IP": "203.0.113.8",
      "Connection": "x-remove-me",
      "X-Remove-Me": "secret",
      "Proxy-Connection": "keep-alive",
    },
    body: createReadStream(MOCK_EXPORT),
  });
  const inspection = await captured.promise;

  assert.equal(inspection.method, "POST");
  assert.equal(inspection.url, "/api/bookmark-imports?source=browser");
  assert.equal(inspection.size, MOCK_EXPORT_SIZE);
  assert.equal(inspection.sha256, MOCK_EXPORT_SHA256);
  assert.equal(inspection.headers.host, new URL(upstreamBaseUrl).host);
  assert.equal(inspection.headers["content-type"], "application/octet-stream");
  assert.equal(inspection.headers["content-length"], String(MOCK_EXPORT_SIZE));
  assert.equal(inspection.headers.cookie, "webhub_session=session-token");
  assert.equal(inspection.headers.origin, gatewayBaseUrl);
  assert.equal(inspection.headers["idempotency-key"], "upload-real-mock-request-0001");
  assert.equal(inspection.headers["x-bookmark-filename"], "bookmarks_2026_7_26.html");
  assert.equal(inspection.headers["x-forwarded-for"], "127.0.0.1");
  assert.equal(inspection.headers["x-forwarded-host"], new URL(gatewayBaseUrl).host);
  assert.equal(inspection.headers["x-forwarded-proto"], "http");
  assert.equal(inspection.headers.forwarded, undefined);
  assert.equal(inspection.headers["x-real-ip"], undefined);
  assert.equal(inspection.headers["x-remove-me"], undefined);
  assert.equal(inspection.headers["proxy-connection"], undefined);

  assert.equal(response.statusCode, 201);
  assert.equal(response.headers.location, "/api/bookmark-imports/job-real-mock");
  assert.deepEqual(response.headers["set-cookie"], [
    "first_cookie=one; Path=/; HttpOnly",
    "second_cookie=two; Path=/; SameSite=Lax",
  ]);
  assert.equal(response.headers["x-upstream-hop"], undefined);
  assert.equal(response.body.toString(), '{"job_id":"job-real-mock"}');
});

test("streams a twelve MiB request without the Next ten MiB truncation", {
  timeout: 15_000,
}, async (context) => {
  const received = deferred();
  const upstream = createServer((request, response) => {
    void inspectRequest(request).then((inspection) => {
      received.resolve(inspection.size);
      response.statusCode = 204;
      response.end();
    }, received.reject);
  });
  const upstreamBaseUrl = await listen(upstream);
  const gateway = createProxyServer({
    target: upstreamBaseUrl,
    maxBytes: 16 * 1024 * 1024,
  });
  const gatewayBaseUrl = await listen(gateway);
  context.after(() => closeServer(gateway));
  context.after(() => closeServer(upstream));

  const chunkSize = 1024 * 1024;
  const bodySize = 12 * chunkSize;
  const body = Readable.from((function* generateBody() {
    for (let index = 0; index < 12; index += 1) yield Buffer.alloc(chunkSize, index);
  }()));
  const response = await sendRequest({
    baseUrl: gatewayBaseUrl,
    path: "/api/backend/bookmark-imports",
    headers: {
      "Content-Type": "application/octet-stream",
      "Content-Length": String(bodySize),
    },
    body,
  });

  assert.equal(response.statusCode, 204);
  assert.equal(await received.promise, bodySize);
});

test("rejects a declared oversized upload before contacting upstream", async (context) => {
  let upstreamRequests = 0;
  const upstream = createServer((request, response) => {
    upstreamRequests += 1;
    request.resume();
    response.statusCode = 204;
    response.end();
  });
  const upstreamBaseUrl = await listen(upstream);
  const gateway = createProxyServer({ target: upstreamBaseUrl, maxBytes: 1024 });
  const gatewayBaseUrl = await listen(gateway);
  context.after(() => closeServer(gateway));
  context.after(() => closeServer(upstream));

  const response = await sendRequest({
    baseUrl: gatewayBaseUrl,
    path: "/api/backend/bookmark-imports",
    headers: {
      "Content-Type": "application/octet-stream",
      "Content-Length": "1025",
    },
    body: Buffer.alloc(1025),
  });

  assert.equal(response.statusCode, 413);
  assert.equal(upstreamRequests, 0);
});

test("rejects a chunked upload as soon as its streamed size exceeds the limit", {
  timeout: 5_000,
}, async (context) => {
  const upstream = createServer((request) => {
    request.on("error", () => {});
    request.resume();
  });
  const upstreamBaseUrl = await listen(upstream);
  const gateway = createProxyServer({ target: upstreamBaseUrl, maxBytes: 1024 });
  const gatewayBaseUrl = await listen(gateway);
  context.after(() => closeServer(gateway));
  context.after(() => closeServer(upstream));

  const body = Readable.from((async function* generateChunkedBody() {
    yield Buffer.alloc(800);
    await new Promise((resolve) => setImmediate(resolve));
    yield Buffer.alloc(800);
  }()));
  const response = await sendRequest({
    baseUrl: gatewayBaseUrl,
    path: "/api/backend/bookmark-imports",
    headers: { "Content-Type": "application/octet-stream" },
    body,
  });

  assert.equal(response.statusCode, 413);
});

test("leaves non-target methods and pathnames on the Next fallback", async (context) => {
  let upstreamRequests = 0;
  let fallbackRequests = 0;
  const upstream = createServer((request, response) => {
    upstreamRequests += 1;
    request.resume();
    response.statusCode = 500;
    response.end();
  });
  const upstreamBaseUrl = await listen(upstream);
  const gateway = createProxyServer({
    target: upstreamBaseUrl,
    fallback(request, response) {
      fallbackRequests += 1;
      request.resume();
      response.statusCode = 202;
      response.end("Next fallback");
    },
  });
  const gatewayBaseUrl = await listen(gateway);
  context.after(() => closeServer(gateway));
  context.after(() => closeServer(upstream));

  const requests = [
    { method: "GET", path: "/api/backend/bookmark-imports" },
    { method: "POST", path: "/api/backend/bookmark-imports/" },
    { method: "POST", path: "/api/backend/other" },
  ];
  for (const request of requests) {
    const response = await sendRequest({ baseUrl: gatewayBaseUrl, ...request });
    assert.equal(response.statusCode, 202);
    assert.equal(response.body.toString(), "Next fallback");
  }
  assert.equal(fallbackRequests, requests.length);
  assert.equal(upstreamRequests, 0);
});

test("returns 502 when the upload upstream resets before responding", async (context) => {
  const upstream = createServer((request) => {
    request.socket.destroy();
  });
  const upstreamBaseUrl = await listen(upstream);
  const gateway = createProxyServer({ target: upstreamBaseUrl, idleTimeoutMs: 1_000 });
  const gatewayBaseUrl = await listen(gateway);
  context.after(() => closeServer(gateway));
  context.after(() => closeServer(upstream));

  const response = await sendRequest({
    baseUrl: gatewayBaseUrl,
    path: "/api/backend/bookmark-imports",
    headers: { "Content-Length": "4", "Content-Type": "text/html" },
    body: "test",
  });
  assert.equal(response.statusCode, 502);
});

test("returns 504 when the upload upstream becomes idle", { timeout: 5_000 }, async (context) => {
  const upstream = createServer((request) => request.resume());
  const upstreamBaseUrl = await listen(upstream);
  const gateway = createProxyServer({ target: upstreamBaseUrl, idleTimeoutMs: 50 });
  const gatewayBaseUrl = await listen(gateway);
  context.after(() => closeServer(gateway));
  context.after(() => closeServer(upstream));

  const response = await sendRequest({
    baseUrl: gatewayBaseUrl,
    path: "/api/backend/bookmark-imports",
    headers: { "Content-Length": "4", "Content-Type": "text/html" },
    body: "test",
  });
  assert.equal(response.statusCode, 504);
});

test("destroys the upstream upload when the browser disconnects", {
  timeout: 5_000,
}, async (context) => {
  const upstreamStarted = deferred();
  const upstreamDisconnected = deferred();
  const upstream = createServer((request) => {
    upstreamStarted.resolve();
    request.on("error", () => {});
    request.once("aborted", upstreamDisconnected.resolve);
    request.once("close", () => {
      if (!request.complete) upstreamDisconnected.resolve();
    });
    request.resume();
  });
  const upstreamBaseUrl = await listen(upstream);
  const gateway = createProxyServer({ target: upstreamBaseUrl, idleTimeoutMs: 1_000 });
  const gatewayBaseUrl = await listen(gateway);
  context.after(() => closeServer(gateway));
  context.after(() => closeServer(upstream));

  const clientRequest = httpRequest(
    new URL("/api/backend/bookmark-imports", gatewayBaseUrl),
    {
      method: "POST",
      headers: {
        "Content-Length": String(10 * 1024 * 1024),
        "Content-Type": "application/octet-stream",
      },
    },
  );
  clientRequest.on("error", () => {});
  clientRequest.write(Buffer.alloc(64 * 1024));
  await upstreamStarted.promise;
  clientRequest.destroy();
  await upstreamDisconnected.promise;
});
