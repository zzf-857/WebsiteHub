import {
  authErrorMessage,
  normalizeAuthUser,
  type AuthUser,
  type ThemeMode,
} from "./auth-contract.ts";

export class AuthApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "AuthApiError";
    this.status = status;
  }
}

async function readPayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) return null;

  try {
    return await response.json();
  } catch {
    return null;
  }
}

export async function requestCurrentUser(signal?: AbortSignal): Promise<AuthUser | null> {
  const response = await fetch("/api/backend/auth/me", {
    cache: "no-store",
    credentials: "include",
    signal,
  });

  if (response.status === 401) return null;

  const payload = await readPayload(response);
  if (!response.ok) throw new AuthApiError(response.status, authErrorMessage(response.status, payload));

  const user = normalizeAuthUser(payload);
  if (!user) throw new AuthApiError(502, "认证服务返回了无法识别的账号信息");
  return user;
}

export async function submitCredentials(
  mode: "login" | "register",
  body: { username: string; password: string; display_name?: string },
): Promise<AuthUser> {
  const response = await fetch(`/api/backend/auth/${mode}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await readPayload(response);

  if (!response.ok) throw new AuthApiError(response.status, authErrorMessage(response.status, payload));

  const user = normalizeAuthUser(payload);
  if (!user) throw new AuthApiError(502, "认证服务返回了无法识别的账号信息");
  return user;
}

export async function submitLogout(): Promise<void> {
  const response = await fetch("/api/backend/auth/logout", {
    method: "POST",
    credentials: "include",
  });

  if (response.ok || response.status === 401) return;
  const payload = await readPayload(response);
  throw new AuthApiError(response.status, authErrorMessage(response.status, payload));
}

export async function updateThemePreference(theme: ThemeMode): Promise<AuthUser> {
  const response = await fetch("/api/backend/auth/preferences", {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ theme }),
  });
  const payload = await readPayload(response);

  if (!response.ok) throw new AuthApiError(response.status, authErrorMessage(response.status, payload));

  const user = normalizeAuthUser(payload);
  if (!user) throw new AuthApiError(502, "认证服务返回了无法识别的账号信息");
  return user;
}
