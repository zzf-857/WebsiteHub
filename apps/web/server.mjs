import { createServer, request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { isIP } from "node:net";
import { hostname as machineHostname, networkInterfaces } from "node:os";
import { Transform } from "node:stream";
import { pathToFileURL } from "node:url";

import next from "next";

export const BOOKMARK_UPLOAD_PROXY_PATH = "/api/backend/bookmark-imports";
export const DEFAULT_BOOKMARK_UPLOAD_MAX_BYTES = 512 * 1024 * 1024;
export const DEFAULT_BOOKMARK_UPLOAD_IDLE_TIMEOUT_MS = 5 * 60 * 1000;
export const DEFAULT_INCOMING_REQUEST_TIMEOUT_MS = 30 * 60 * 1000;

const DEFAULT_API_INTERNAL_URL = "http://127.0.0.1:8100";
const DEFAULT_HEADERS_TIMEOUT_MS = 60 * 1000;
const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "expect",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

class BookmarkUploadTooLargeError extends Error {
  constructor(maxBytes) {
    super(`Bookmark upload exceeds the ${maxBytes}-byte proxy limit`);
    this.name = "BookmarkUploadTooLargeError";
  }
}

class BookmarkUploadLimitStream extends Transform {
  #bytesRead = 0;

  constructor(maxBytes) {
    super();
    this.maxBytes = maxBytes;
  }

  _transform(chunk, encoding, callback) {
    const byteLength = Buffer.isBuffer(chunk) ? chunk.length : Buffer.byteLength(chunk, encoding);
    if (byteLength > this.maxBytes - this.#bytesRead) {
      callback(new BookmarkUploadTooLargeError(this.maxBytes));
      return;
    }
    this.#bytesRead += byteLength;
    callback(null, chunk);
  }
}

function optionValue(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

export function isDevelopmentMode(args) {
  return args.includes("--dev");
}

export function normalizeRemoteAddress(remoteAddress) {
  if (!remoteAddress) return null;
  const withoutZone = remoteAddress.split("%", 1)[0];
  const candidate = withoutZone.startsWith("::ffff:")
    ? withoutZone.slice("::ffff:".length)
    : withoutZone;
  return isIP(candidate) ? candidate : null;
}

export function normalizeHostHeader(host) {
  if (typeof host !== "string" || !host || /\s/.test(host)) return null;
  try {
    const parsed = new URL(`http://${host}`);
    if (
      parsed.username
      || parsed.password
      || parsed.pathname !== "/"
      || parsed.search
      || parsed.hash
    ) return null;
    return parsed.hostname.toLowerCase();
  } catch {
    return null;
  }
}

function normalizedAddressHost(address) {
  const normalized = normalizeRemoteAddress(address);
  if (!normalized) return null;
  return normalized.includes(":") ? `[${normalized}]` : normalized;
}

export function buildAllowedHosts({
  configured = process.env.WEBHUB_ALLOWED_HOSTS ?? "",
  interfaces = networkInterfaces(),
  hostname = machineHostname(),
} = {}) {
  const allowed = new Set(["localhost", "127.0.0.1", "[::1]"]);
  const normalizedMachineHostname = normalizeHostHeader(hostname);
  if (normalizedMachineHostname) allowed.add(normalizedMachineHostname);

  for (const addresses of Object.values(interfaces)) {
    for (const entry of addresses ?? []) {
      const normalized = normalizedAddressHost(entry.address);
      if (normalized) allowed.add(normalized);
    }
  }
  for (const entry of configured.split(",")) {
    const normalized = normalizeHostHeader(entry.trim());
    if (normalized) allowed.add(normalized);
  }
  return allowed;
}

export function isAllowedHost(host, allowedHosts) {
  const normalized = normalizeHostHeader(host);
  return normalized !== null && allowedHosts.has(normalized);
}

export async function prepareNextApplication({ dev, hostname, port, createApplication = next }) {
  const app = createApplication({ dev, hostname, port });
  await app.prepare();
  return { app, allowedHosts: buildAllowedHosts() };
}

function forwardedPort(host, localPort) {
  if (host) {
    try {
      const parsed = new URL(`http://${host}`);
      if (parsed.port) return parsed.port;
    } catch {
      // Fall through to the trusted socket port.
    }
  }
  return localPort ? String(localPort) : "";
}

export function overwriteForwardingHeaders(headers, socket) {
  const remoteAddress = normalizeRemoteAddress(socket.remoteAddress);
  const host = typeof headers.host === "string" ? headers.host : "";

  delete headers.forwarded;
  delete headers["x-real-ip"];

  if (remoteAddress) headers["x-forwarded-for"] = remoteAddress;
  else delete headers["x-forwarded-for"];

  if (host) headers["x-forwarded-host"] = host;
  else delete headers["x-forwarded-host"];

  headers["x-forwarded-proto"] = socket.encrypted ? "https" : "http";
  const port = forwardedPort(host, socket.localPort);
  if (port) headers["x-forwarded-port"] = port;
  else delete headers["x-forwarded-port"];
}

function assertPositiveSafeInteger(value, name) {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new TypeError(`${name} must be a positive safe integer`);
  }
}

function connectionHeaderNames(headers) {
  const value = headers.connection;
  const entries = Array.isArray(value) ? value : [value];
  return entries
    .filter((entry) => typeof entry === "string")
    .flatMap((entry) => entry.split(","))
    .map((entry) => entry.trim().toLowerCase())
    .filter(Boolean);
}

function withoutHopByHopHeaders(headers) {
  const blocked = new Set([
    ...HOP_BY_HOP_HEADERS,
    ...connectionHeaderNames(headers),
  ]);
  const forwarded = {};
  for (const [name, value] of Object.entries(headers)) {
    if (value !== undefined && !blocked.has(name.toLowerCase())) {
      forwarded[name] = value;
    }
  }
  return forwarded;
}

function bookmarkUploadRequestSearch(requestUrl) {
  const queryIndex = requestUrl.indexOf("?");
  return queryIndex === -1 ? "" : requestUrl.slice(queryIndex);
}

export function isBookmarkUploadRequest(request) {
  if (request.method !== "POST" || typeof request.url !== "string") return false;
  if (!request.url.startsWith("/")) return false;
  const queryIndex = request.url.indexOf("?");
  const pathname = queryIndex === -1 ? request.url : request.url.slice(0, queryIndex);
  return pathname === BOOKMARK_UPLOAD_PROXY_PATH;
}

export function buildBookmarkUploadTarget(
  apiBaseUrl = process.env.WEBHUB_API_INTERNAL_URL ?? DEFAULT_API_INTERNAL_URL,
) {
  let target;
  try {
    target = new URL(apiBaseUrl);
  } catch (error) {
    throw new TypeError("WEBHUB_API_INTERNAL_URL must be a valid absolute URL", { cause: error });
  }
  if (!new Set(["http:", "https:"]).has(target.protocol)) {
    throw new TypeError("WEBHUB_API_INTERNAL_URL must use http or https");
  }
  if (target.username || target.password || target.search || target.hash) {
    throw new TypeError("WEBHUB_API_INTERNAL_URL cannot contain credentials, query, or fragment");
  }
  target.pathname = `${target.pathname.replace(/\/+$/, "")}/api/bookmark-imports`;
  return target;
}

function declaredContentLength(headers) {
  const value = headers["content-length"];
  if (value === undefined) return null;
  if (Array.isArray(value) || !/^\d+$/.test(value)) return Number.NaN;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : Number.NaN;
}

function sendProxyError(response, statusCode, message) {
  if (response.destroyed || response.writableEnded) return;
  if (response.headersSent) {
    response.destroy();
    return;
  }
  response.statusCode = statusCode;
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("Connection", "close");
  response.setHeader("Content-Type", "text/plain; charset=utf-8");
  response.end(message);
}

function applyUpstreamResponseHeaders(upstreamResponse, response) {
  const headers = withoutHopByHopHeaders(upstreamResponse.headers);
  for (const [name, value] of Object.entries(headers)) {
    if (value !== undefined) response.setHeader(name, value);
  }
}

export function createBookmarkUploadProxy({
  target,
  maxBytes = DEFAULT_BOOKMARK_UPLOAD_MAX_BYTES,
  idleTimeoutMs = DEFAULT_BOOKMARK_UPLOAD_IDLE_TIMEOUT_MS,
  httpRequestImplementation = httpRequest,
  httpsRequestImplementation = httpsRequest,
} = {}) {
  assertPositiveSafeInteger(maxBytes, "maxBytes");
  assertPositiveSafeInteger(idleTimeoutMs, "idleTimeoutMs");
  const uploadTarget = target instanceof URL
    ? new URL(target)
    : buildBookmarkUploadTarget(target);
  if (!new Set(["http:", "https:"]).has(uploadTarget.protocol)) {
    throw new TypeError("Bookmark upload target must use http or https");
  }
  if (uploadTarget.username || uploadTarget.password || uploadTarget.search || uploadTarget.hash) {
    throw new TypeError("Bookmark upload target cannot contain credentials, query, or fragment");
  }

  return function proxyBookmarkUpload(request, response) {
    if (!isBookmarkUploadRequest(request)) return false;

    const contentLength = declaredContentLength(request.headers);
    if (Number.isNaN(contentLength)) {
      sendProxyError(response, 400, "Invalid Content-Length");
      return true;
    }
    if (contentLength !== null && contentLength > maxBytes) {
      request.resume();
      sendProxyError(response, 413, "Bookmark upload is too large");
      return true;
    }

    const headers = withoutHopByHopHeaders(request.headers);
    headers.host = uploadTarget.host;
    const requestImplementation = uploadTarget.protocol === "https:"
      ? httpsRequestImplementation
      : httpRequestImplementation;
    const options = {
      protocol: uploadTarget.protocol,
      hostname: uploadTarget.hostname,
      port: uploadTarget.port || undefined,
      method: "POST",
      path: `${uploadTarget.pathname}${bookmarkUploadRequestSearch(request.url)}`,
      headers,
    };

    let completed = false;
    let clientDisconnected = false;
    let upstreamRequest;
    let upstreamResponse;
    let limiter;

    const stopForwarding = (error) => {
      request.unpipe();
      if (limiter) limiter.unpipe();
      if (limiter && !limiter.destroyed) limiter.destroy();
      if (upstreamResponse && !upstreamResponse.destroyed) upstreamResponse.destroy(error);
      if (upstreamRequest && !upstreamRequest.destroyed) upstreamRequest.destroy(error);
    };
    const fail = (statusCode, message) => {
      if (completed || clientDisconnected) return;
      completed = true;
      stopForwarding();
      request.resume();
      sendProxyError(response, statusCode, message);
    };
    const disconnectUpstream = (error) => {
      clientDisconnected = true;
      stopForwarding(error);
    };

    request.once("aborted", disconnectUpstream);
    request.once("error", disconnectUpstream);
    response.once("close", () => {
      if (!response.writableEnded) disconnectUpstream();
    });
    response.once("finish", () => {
      completed = true;
    });

    try {
      upstreamRequest = requestImplementation(options, (incomingResponse) => {
        upstreamResponse = incomingResponse;
        if (completed || clientDisconnected || response.destroyed || response.writableEnded) {
          incomingResponse.destroy();
          return;
        }
        response.statusCode = incomingResponse.statusCode ?? 502;
        if (incomingResponse.statusMessage) response.statusMessage = incomingResponse.statusMessage;
        applyUpstreamResponseHeaders(incomingResponse, response);
        incomingResponse.once("aborted", () => fail(502, "Bookmark upload upstream disconnected"));
        incomingResponse.once("error", () => fail(502, "Bookmark upload upstream failed"));
        incomingResponse.pipe(response);
      });
    } catch {
      fail(502, "Bookmark upload upstream is unavailable");
      return true;
    }

    upstreamRequest.setTimeout(idleTimeoutMs, () => {
      fail(504, "Bookmark upload upstream timed out");
    });
    upstreamRequest.once("error", (error) => {
      if (error instanceof BookmarkUploadTooLargeError) return;
      fail(
        error?.code === "ETIMEDOUT" ? 504 : 502,
        error?.code === "ETIMEDOUT"
          ? "Bookmark upload upstream timed out"
          : "Bookmark upload upstream is unavailable",
      );
    });

    if (contentLength === null) {
      limiter = new BookmarkUploadLimitStream(maxBytes);
      limiter.once("error", () => {
        fail(413, "Bookmark upload is too large");
      });
      request.pipe(limiter).pipe(upstreamRequest);
    } else {
      request.pipe(upstreamRequest);
    }
    return true;
  };
}

export async function startServer() {
  const dev = isDevelopmentMode(process.argv);
  const hostname = optionValue("--hostname", process.env.HOSTNAME || "0.0.0.0");
  const port = Number.parseInt(optionValue("--port", process.env.PORT || "3100"), 10);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("PORT must be an integer between 1 and 65535");
  }

  const { app, allowedHosts } = await prepareNextApplication({ dev, hostname, port });
  const handle = app.getRequestHandler();
  const handleUpgrade = app.getUpgradeHandler();
  const proxyBookmarkUpload = createBookmarkUploadProxy({
    target: buildBookmarkUploadTarget(),
  });

  const server = createServer((request, response) => {
    if (!isAllowedHost(request.headers.host, allowedHosts)) {
      response.statusCode = 421;
      response.setHeader("Connection", "close");
      response.end("Misdirected Request");
      return;
    }
    // Establish a single trusted proxy hop before Next can preserve forwarded input.
    overwriteForwardingHeaders(request.headers, request.socket);
    if (proxyBookmarkUpload(request, response)) return;
    void handle(request, response).catch((error) => {
      console.error(error);
      if (!response.headersSent) response.statusCode = 500;
      response.end("Internal Server Error");
    });
  });
  // The Node default is five minutes, which is too short for a valid 512 MiB upload.
  server.requestTimeout = DEFAULT_INCOMING_REQUEST_TIMEOUT_MS;
  server.headersTimeout = DEFAULT_HEADERS_TIMEOUT_MS;

  server.on("upgrade", (request, socket, head) => {
    if (!isAllowedHost(request.headers.host, allowedHosts)) {
      socket.end(
        "HTTP/1.1 421 Misdirected Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
      );
      return;
    }
    overwriteForwardingHeaders(request.headers, request.socket);
    void handleUpgrade(request, socket, head).catch(() => socket.destroy());
  });

  await new Promise((resolve, reject) => {
    const onError = (error) => reject(error);
    server.once("error", onError);
    server.listen(port, hostname, () => {
      server.off("error", onError);
      resolve();
    });
  });

  console.log(`WebHub website ready on http://${hostname}:${port}`);
  return server;
}

const entryPath = process.argv[1] ? pathToFileURL(process.argv[1]).href : null;
if (entryPath === import.meta.url) {
  startServer().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
