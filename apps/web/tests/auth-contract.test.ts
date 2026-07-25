import assert from "node:assert/strict";
import test from "node:test";

import {
  authErrorMessage,
  normalizeAuthUser,
  parseThemeMode,
  safeNextPath,
  validateAuthForm,
} from "../lib/auth-contract.ts";
import { readLocalTheme, writeLocalTheme } from "../lib/theme.ts";

test("normalizes the backend auth response and account preferences", () => {
  assert.deepEqual(
    normalizeAuthUser({
      user: {
        id: "user-1",
        username: " alice ",
        display_name: " Alice ",
        preferences: { theme: "dark", locale: "zh-CN" },
      },
    }),
    {
      id: "user-1",
      username: "alice",
      displayName: "Alice",
      preferences: { theme: "dark", locale: "zh-CN" },
    },
  );
});

test("uses safe preference defaults for malformed optional data", () => {
  assert.deepEqual(normalizeAuthUser({ username: "alice" })?.preferences, {
    theme: "system",
    locale: "zh-CN",
  });
  assert.equal(parseThemeMode("purple"), "system");
});

test("validates registration constraints and matching passwords", () => {
  const errors = validateAuthForm("register", {
    username: "a b",
    password: "short",
    confirmPassword: "different",
    displayName: "x".repeat(81),
  });

  assert.equal(errors.username, "用户名不能包含空格");
  assert.equal(errors.password, "密码至少需要 8 个字符");
  assert.equal(errors.confirmPassword, "两次输入的密码不一致");
  assert.equal(errors.displayName, "显示名称不能超过 80 个字符");
  assert.equal(
    validateAuthForm("register", {
      username: "a".repeat(33),
      password: "valid password",
      confirmPassword: "valid password",
      displayName: "",
    }).username,
    "用户名不能超过 32 个字符",
  );
});

test("keeps redirects on local website routes", () => {
  assert.equal(safeNextPath("/library?category=ai"), "/library?category=ai");
  assert.equal(safeNextPath("//attacker.invalid"), "/chat/new");
  assert.equal(safeNextPath("/\\attacker.invalid"), "/chat/new");
  assert.equal(safeNextPath("https://attacker.invalid"), "/chat/new");
  assert.equal(safeNextPath("/login"), "/chat/new");
  assert.equal(safeNextPath("/login/"), "/chat/new");
  assert.equal(safeNextPath("/login?next=/library"), "/chat/new");
  assert.equal(safeNextPath("/register/"), "/chat/new");
  assert.equal(safeNextPath("/register?next=/spaces"), "/chat/new");
});

test("extracts useful API errors and falls back by status", () => {
  assert.equal(authErrorMessage(422, { detail: [{ msg: "字段无效" }] }), "字段无效");
  assert.equal(authErrorMessage(409, null), "该用户名已被使用");
  assert.equal(authErrorMessage(503, null), "认证服务暂时不可用，请稍后重试");
});

test("theme storage helpers tolerate blocked or exhausted browser storage", () => {
  const blockedRead = { getItem: () => { throw new Error("blocked"); } };
  const blockedWrite = { setItem: () => { throw new Error("quota"); } };

  assert.equal(readLocalTheme(blockedRead), "system");
  assert.equal(writeLocalTheme(blockedWrite, "dark"), false);
});
