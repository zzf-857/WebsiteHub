import assert from "node:assert/strict";
import test from "node:test";

import { requestCurrentUser, submitCredentials } from "../lib/auth-client.ts";

const authPayload = {
  user: {
    id: "user-1",
    username: "alice",
    display_name: "Alice",
    preferences: { theme: "dark", locale: "zh-CN" },
  },
};

test("credential submission includes cookies and returns the authenticated account", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let capturedInput: string | URL | Request | undefined;
  let capturedInit: RequestInit | undefined;

  globalThis.fetch = async (input, init) => {
    capturedInput = input;
    capturedInit = init;
    return new Response(JSON.stringify(authPayload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  const user = await submitCredentials("login", {
    username: "alice",
    password: "correct password",
  });

  assert.equal(capturedInput, "/api/backend/auth/login");
  assert.equal(capturedInit?.credentials, "include");
  assert.equal(capturedInit?.method, "POST");
  assert.equal(user.username, "alice");
  assert.equal(user.preferences.theme, "dark");
});

test("an unauthorized current-user response becomes an anonymous session", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });

  globalThis.fetch = async () => new Response(null, { status: 401 });

  assert.equal(await requestCurrentUser(), null);
});
