import { createServer } from "node:http";
import { isIP } from "node:net";
import { hostname as machineHostname, networkInterfaces } from "node:os";
import { pathToFileURL } from "node:url";

import next from "next";

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

  const server = createServer((request, response) => {
    if (!isAllowedHost(request.headers.host, allowedHosts)) {
      response.statusCode = 421;
      response.setHeader("Connection", "close");
      response.end("Misdirected Request");
      return;
    }
    // Establish a single trusted proxy hop before Next can preserve forwarded input.
    overwriteForwardingHeaders(request.headers, request.socket);
    void handle(request, response).catch((error) => {
      console.error(error);
      if (!response.headersSent) response.statusCode = 500;
      response.end("Internal Server Error");
    });
  });

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
