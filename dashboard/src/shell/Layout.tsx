import { useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";

import {
  applyPrefs,
  currentDensity,
  currentTheme,
  saveDensity,
  saveTheme,
  type Density,
  type Theme,
} from "../prefs";

const NATIVE_PAGES = [
  { to: "/", label: "Home" },
  { to: "/plugins", label: "Plugins" },
  { to: "/surfaces", label: "Surfaces" },
  { to: "/scopes", label: "Scopes" },
];

export function Layout(props: { title: string; children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(currentTheme);
  const [density, setDensity] = useState<Density>(currentDensity);

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
