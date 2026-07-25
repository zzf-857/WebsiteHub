export type ThemeMode = "system" | "light" | "dark";

export type UserPreferences = {
  theme: ThemeMode;
  locale: string;
};

export type AuthUser = {
  id?: string;
  username: string;
  displayName: string | null;
  preferences: UserPreferences;
};

export type AuthMode = "login" | "register";

export type AuthFormValues = {
  username: string;
  password: string;
  confirmPassword: string;
  displayName: string;
};

export type AuthFieldErrors = Partial<Record<keyof AuthFormValues, string>>;

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseThemeMode(value: unknown): ThemeMode {
  return value === "light" || value === "dark" || value === "system" ? value : "system";
}

export function normalizeAuthUser(payload: unknown): AuthUser | null {
  if (!isRecord(payload)) return null;

  const candidate = isRecord(payload.user)
    ? payload.user
    : isRecord(payload.account)
      ? payload.account
      : payload;
  const username = typeof candidate.username === "string" ? candidate.username.trim() : "";

  if (!username) return null;

  const rawDisplayName = candidate.display_name ?? candidate.displayName;
  const rawId = candidate.id ?? candidate.user_id;
  const preferences = isRecord(candidate.preferences) ? candidate.preferences : {};

  return {
    id: typeof rawId === "string" || typeof rawId === "number" ? String(rawId) : undefined,
    username,
    displayName:
      typeof rawDisplayName === "string" && rawDisplayName.trim()
        ? rawDisplayName.trim()
        : null,
    preferences: {
      theme: parseThemeMode(preferences.theme),
      locale:
        typeof preferences.locale === "string" && preferences.locale.trim()
          ? preferences.locale.trim()
          : "zh-CN",
    },
  };
}

export function validateAuthForm(mode: AuthMode, values: AuthFormValues): AuthFieldErrors {
  const errors: AuthFieldErrors = {};
  const username = values.username.trim();
  const displayName = values.displayName.trim();

  if (!username) {
    errors.username = "请输入用户名";
  } else if (mode === "register" && username.length < 3) {
    errors.username = "用户名至少需要 3 个字符";
  } else if (username.length > (mode === "register" ? 32 : 64)) {
    errors.username = `用户名不能超过 ${mode === "register" ? 32 : 64} 个字符`;
  } else if (/\s/.test(username)) {
    errors.username = "用户名不能包含空格";
  }

  if (!values.password) {
    errors.password = "请输入密码";
  } else if (mode === "register" && values.password.length < 8) {
    errors.password = "密码至少需要 8 个字符";
  } else if (values.password.length > 128) {
    errors.password = "密码不能超过 128 个字符";
  }

  if (mode === "register") {
    if (!values.confirmPassword) {
      errors.confirmPassword = "请再次输入密码";
    } else if (values.confirmPassword !== values.password) {
      errors.confirmPassword = "两次输入的密码不一致";
    }

    if (displayName.length > 80) {
      errors.displayName = "显示名称不能超过 80 个字符";
    }
  }

  return errors;
}

export function safeNextPath(value: string | null | undefined): string {
  const fallback = "/chat/new";
  if (!value) return fallback;

  try {
    const base = new URL("http://webhub.local");
    const target = new URL(value, base);
    if (target.origin !== base.origin) return fallback;
    const normalizedPathname = target.pathname.replace(/\/+$/, "") || "/";
    if (normalizedPathname === "/login" || normalizedPathname === "/register") return fallback;
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return fallback;
  }
}

export function authErrorMessage(status: number, payload: unknown): string {
  if (isRecord(payload)) {
    if (typeof payload.detail === "string" && payload.detail.trim()) return payload.detail;
    if (typeof payload.message === "string" && payload.message.trim()) return payload.message;
    if (Array.isArray(payload.detail)) {
      const first = payload.detail.find(isRecord);
      if (first && typeof first.msg === "string" && first.msg.trim()) return first.msg;
    }
  }

  if (status === 401) return "用户名或密码错误";
  if (status === 409) return "该用户名已被使用";
  if (status === 422) return "提交的信息不符合要求";
  if (status === 429) return "请求过于频繁，请稍后再试";
  return "认证服务暂时不可用，请稍后重试";
}
