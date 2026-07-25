import { parseThemeMode, type ThemeMode } from "./auth-contract.ts";

export const THEME_STORAGE_KEY = "webhub-theme";

export function readLocalTheme(storage: Pick<Storage, "getItem">): ThemeMode {
  try {
    return parseThemeMode(storage.getItem(THEME_STORAGE_KEY));
  } catch {
    return "system";
  }
}

export function readBrowserTheme(): ThemeMode {
  try {
    return readLocalTheme(window.localStorage);
  } catch {
    return "system";
  }
}

export function applyDocumentTheme(theme: ThemeMode): void {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = theme;
}

export function writeLocalTheme(
  storage: Pick<Storage, "setItem">,
  theme: ThemeMode,
): boolean {
  try {
    storage.setItem(THEME_STORAGE_KEY, theme);
    return true;
  } catch {
    return false;
  }
}

export function persistLocalTheme(theme: ThemeMode): void {
  try {
    writeLocalTheme(window.localStorage, theme);
  } catch {
    // Account preferences remain authoritative when browser storage is unavailable.
  }
}
