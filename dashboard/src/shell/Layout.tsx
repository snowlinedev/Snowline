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
import { SECTIONS, pluginNavGroups, sectionNavEntries } from "../registry";
import { useData } from "../useData";

export function Layout(props: { title: string; children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(currentTheme);
  const [density, setDensity] = useState<Density>(currentDensity);
  // Narrow-viewport nav disclosure (issue #161): the links container is
  // CSS-collapsed below 640px unless open. State lives here so the button
  // can announce it (aria-expanded) and navigation can close it; on desktop
  // the button is display:none and the links are always shown, so this
  // state is inert there.
  const [navOpen, setNavOpen] = useState(false);
  const closeNav = () => setNavOpen(false);
  // Demand-side nav (dashboard-ia.md §3): the five platform-owned sections,
  // each listing the contributions whose `intent` placed them there, then the
  // per-plugin fallback groups for everything that declared no intent. Every
  // page renders through Layout, so this one fetch/render path is how ALL nav
  // stays in sync with the live plugin registry. Sections render from the
  // static table even before the fetch resolves — the chrome is stable, only
  // its tenants arrive late.
  const plugins = useData(fetchPlugins, 30);
  const registry = plugins.state === "ready" ? plugins.data : [];
  const navGroups = pluginNavGroups(registry);

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
      <nav
        className={navOpen ? "shell-nav nav-open" : "shell-nav"}
        aria-label="Main"
      >
        <div className="shell-nav-top">
          <div className="brand">Snowline</div>
          <button
            type="button"
            className="nav-toggle"
            aria-expanded={navOpen}
            aria-controls="shell-nav-links"
            onClick={() => setNavOpen(!navOpen)}
          >
            Menu
          </button>
        </div>
        <div className="shell-nav-links" id="shell-nav-links">
          {SECTIONS.map((section) => {
            const entries = sectionNavEntries(registry, section.id);
            return (
              <div className="nav-section" key={section.id}>
                <NavLink
                  to={section.to}
                  end={section.to === "/"}
                  onClick={closeNav}
                >
                  {section.label}
                </NavLink>
                {entries.length > 0 && (
                  <div className="nav-section-pages">
                    {entries.map((e) => (
                      <NavLink key={e.key} to={e.to} onClick={closeNav}>
                        {e.label}
                      </NavLink>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
          {navGroups.map((group) => (
            <div className="nav-group" key={group.plugin}>
              <p className="nav-group-heading">{group.plugin}</p>
              {group.pages.map((p) => (
                <NavLink key={p.to} to={p.to} onClick={closeNav}>
                  {p.label}
                </NavLink>
              ))}
            </div>
          ))}
        </div>
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
