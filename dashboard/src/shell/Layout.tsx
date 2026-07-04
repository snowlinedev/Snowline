import { useEffect, useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { fetchPlugins } from "../api";
import {
  applyPrefs,
  currentDensity,
  currentTheme,
  saveDensity,
  saveTheme,
  type Density,
  type Theme,
} from "../prefs";
import { pluginNavGroups } from "../registry";
import { useData } from "../useData";

const NATIVE_PAGES = [
  { to: "/", label: "Home" },
  { to: "/plugins", label: "Plugins" },
  { to: "/surfaces", label: "Surfaces" },
  { to: "/scopes", label: "Scopes" },
];

export function Layout(props: { title: string; children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(currentTheme);
  const [density, setDensity] = useState<Density>(currentDensity);
  // Registered nav (ui-shell.md §6): native pages first, then `nav: true`
  // plugin pages grouped under a small per-plugin heading. Every page
  // renders through Layout, so this one fetch/render path is how ALL nav —
  // native and registered — stays in sync with the live plugin registry.
  const plugins = useData(fetchPlugins, 30);
  const navGroups = plugins.state === "ready" ? pluginNavGroups(plugins.data) : [];

  // Page titled (WCAG 2.4.2): SPA route changes must retitle the document —
  // tabs, history, and screen readers all read this, not the <h1>.
  useEffect(() => {
    document.title = `${props.title} · Snowline`;
  }, [props.title]);

  const setAndApply = (t: Theme, d: Density) => {
    setTheme(t);
    setDensity(d);
    saveTheme(t);
    saveDensity(d);
    applyPrefs(t, d);
  };

  return (
    <div className="shell">
      <nav className="shell-nav" aria-label="Main">
        <div className="brand">Snowline</div>
        {NATIVE_PAGES.map((p) => (
          <NavLink key={p.to} to={p.to} end={p.to === "/"}>
            {p.label}
          </NavLink>
        ))}
        {navGroups.map((group) => (
          <div className="nav-group" key={group.plugin}>
            <p className="nav-group-heading">{group.plugin}</p>
            {group.pages.map((p) => (
              <NavLink key={p.to} to={p.to}>
                {p.label}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>
      <main className="shell-main">
        <div className="shell-header">
          <h1>{props.title}</h1>
          <div className="toggles">
            <button
              type="button"
              aria-pressed={theme === "dark"}
              onClick={() =>
                setAndApply(theme === "dark" ? "light" : "dark", density)
              }
            >
              Dark theme
            </button>
            <button
              type="button"
              aria-pressed={density === "compact"}
              onClick={() =>
                setAndApply(
                  theme,
                  density === "compact" ? "comfortable" : "compact",
                )
              }
            >
              Compact
            </button>
          </div>
        </div>
        {props.children}
      </main>
    </div>
  );
}
