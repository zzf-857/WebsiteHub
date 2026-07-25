import assert from "node:assert/strict";
import test from "node:test";

import {
  buildAllowedHosts,
  isDevelopmentMode,
  isAllowedHost,
  normalizeHostHeader,
  normalizeRemoteAddress,
  overwriteForwardingHeaders,
  prepareNextApplication,
} from "../server.mjs";

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
