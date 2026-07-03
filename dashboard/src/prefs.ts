/** Theme + density preferences (ui-shell.md §7): two independent axes, each a
 * single attribute on <html>. Density is the user-opted compact trade
 * documented in ACCESSIBILITY.md; theme defaults to the OS preference until
 * explicitly chosen. Both persist in localStorage. */

export type Theme = "light" | "dark";
export type Density = "comfortable" | "compact";

const THEME_KEY = "snowline.theme";
const DENSITY_KEY = "snowline.density";

export function currentTheme(): Theme {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function currentDensity(): Density {
  return localStorage.getItem(DENSITY_KEY) === "compact"
    ? "compact"
    : "comfortable";
}

export function applyPrefs(theme: Theme, density: Density): void {
  document.documentElement.setAttribute("data-theme", theme);
  document.documentElement.setAttribute("data-density", density);
}

export function saveTheme(theme: Theme): void {
  localStorage.setItem(THEME_KEY, theme);
}

export function saveDensity(density: Density): void {
  localStorage.setItem(DENSITY_KEY, density);
}
