"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  AuthApiError,
  requestCurrentUser,
  submitLogout,
  updateThemePreference,
} from "@/lib/auth-client";
import type { AuthUser, ThemeMode } from "@/lib/auth-contract";
import { applyDocumentTheme, persistLocalTheme, readBrowserTheme } from "@/lib/theme";

type AuthState =
  | { status: "loading"; user: null; error: null }
  | { status: "authenticated"; user: AuthUser; error: null }
  | { status: "anonymous"; user: null; error: null }
  | { status: "error"; user: null; error: string };

type AuthContextValue = AuthState & {
  theme: ThemeMode;
  establishSession: (user: AuthUser) => void;
  refresh: () => Promise<AuthUser | null>;
  logout: () => Promise<void>;
  setTheme: (theme: ThemeMode) => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

function messageFromUnknown(error: unknown): string {
  return error instanceof Error ? error.message : "无法连接认证服务";
}

export function AuthProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [state, setState] = useState<AuthState>({ status: "loading", user: null, error: null });
  const [theme, setThemeState] = useState<ThemeMode>("system");
  const themeMutationRef = useRef(0);
  const themeQueueRef = useRef<Promise<void>>(Promise.resolve());

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    const localTheme = readBrowserTheme();
    applyDocumentTheme(localTheme);
    window.queueMicrotask(() => {
      if (active) setThemeState(localTheme);
    });

    void requestCurrentUser(controller.signal)
      .then((user) => {
        if (!active) return;
        if (user) {
          setThemeState(user.preferences.theme);
          persistLocalTheme(user.preferences.theme);
          applyDocumentTheme(user.preferences.theme);
        }
        setState(
          user
            ? { status: "authenticated", user, error: null }
            : { status: "anonymous", user: null, error: null },
        );
      })
      .catch((error: unknown) => {
        if (active && !controller.signal.aborted) {
          setState({ status: "error", user: null, error: messageFromUnknown(error) });
        }
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, []);

  const refresh = useCallback(async () => {
    themeMutationRef.current += 1;
    setState({ status: "loading", user: null, error: null });
    try {
      const user = await requestCurrentUser();
      if (user) {
        setThemeState(user.preferences.theme);
        persistLocalTheme(user.preferences.theme);
        applyDocumentTheme(user.preferences.theme);
      }
      setState(
        user
          ? { status: "authenticated", user, error: null }
          : { status: "anonymous", user: null, error: null },
      );
      return user;
    } catch (error) {
      setState({ status: "error", user: null, error: messageFromUnknown(error) });
      throw error;
    }
  }, []);

  const logout = useCallback(async () => {
    themeMutationRef.current += 1;
    await submitLogout();
    setState({ status: "anonymous", user: null, error: null });
  }, []);

  const establishSession = useCallback((user: AuthUser) => {
    themeMutationRef.current += 1;
    setThemeState(user.preferences.theme);
    persistLocalTheme(user.preferences.theme);
    applyDocumentTheme(user.preferences.theme);
    setState({ status: "authenticated", user, error: null });
  }, []);

  const setTheme = useCallback(
    async (nextTheme: ThemeMode) => {
      const previousTheme = theme;
      setThemeState(nextTheme);
      persistLocalTheme(nextTheme);
      applyDocumentTheme(nextTheme);

      if (state.status !== "authenticated") return;

      const mutationId = ++themeMutationRef.current;
      const accountKey = state.user.id ?? state.user.username;
      let resolveUpdate!: (user: AuthUser | null) => void;
      let rejectUpdate!: (error: unknown) => void;
      const update = new Promise<AuthUser | null>((resolve, reject) => {
        resolveUpdate = resolve;
        rejectUpdate = reject;
      });

      themeQueueRef.current = themeQueueRef.current
        .then(async () => {
          if (mutationId !== themeMutationRef.current) {
            resolveUpdate(null);
            return;
          }
          try {
            resolveUpdate(await updateThemePreference(nextTheme));
          } catch (error) {
            rejectUpdate(error);
          }
        })
        .catch(() => undefined);

      try {
        const user = await update;
        if (mutationId !== themeMutationRef.current || !user) return;
        setState((current) => {
          if (
            current.status !== "authenticated"
            || (current.user.id ?? current.user.username) !== accountKey
          ) return current;
          return { status: "authenticated", user, error: null };
        });
      } catch (error) {
        if (mutationId !== themeMutationRef.current) throw error;
        if (error instanceof AuthApiError && error.status === 401) {
          themeMutationRef.current += 1;
          setState({ status: "anonymous", user: null, error: null });
        }
        setThemeState(previousTheme);
        persistLocalTheme(previousTheme);
        applyDocumentTheme(previousTheme);
        throw error;
      }
    },
    [state, theme],
  );

  const value = useMemo<AuthContextValue>(
    () => ({ ...state, theme, establishSession, refresh, logout, setTheme }),
    [establishSession, logout, refresh, setTheme, state, theme],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider");
  return context;
}
