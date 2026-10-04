import { useEffect, useMemo, useState, type ReactNode } from "react";
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
import { usePlugins } from "../plugins-context";
import { SECTIONS, pluginNavGroups, sectionNavEntries } from "../registry";

export function Layout(props: {
  title: string;
  /** Optional one-line secondary text rendered under the title (ui-shell.md §4.2 `page_subtitle`). */
  subtitle?: string;
  children: ReactNode;
}) {
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
  // page renders through Layout and shares App's one /plugins fetch
  // (plugins-context.tsx), so nav and page content derive from the SAME
  // result. Sections render from the static table even before the fetch
  // resolves — the chrome is stable, only its tenants arrive late. The
  // bucketing memoizes on the fetch's data (stable between polls), not on the
  // per-render Loadable wrapper.
  const plugins = usePlugins();
  const registry = plugins.state === "ready" ? plugins.data : null;
  const sectionEntries = useMemo(
    () => new Map(SECTIONS.map((s) => [s.id, sectionNavEntries(registry ?? [], s)])),
    [registry],
  );
  const navGroups = useMemo(() => pluginNavGroups(registry ?? []), [registry]);

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
            const entries = sectionEntries.get(section.id) ?? [];
            return (
              <div className="nav-section" key={section.id}>
                {/* `end` on every section link: sections have no legitimate
                 * child routes, so a deeper URL (a stale citation, or a
                 * plugin route) must not leave the section marked
                 * aria-current="page" by prefix match. */}
                <NavLink to={section.to} end onClick={closeNav}>
                  {section.label}
                </NavLink>
                {entries.length > 0 && (
                  /* role="group" + label: the tenant/section association is
                   * otherwise only CSS indentation, which assistive tech
                   * flattens into one run of links (WCAG 1.3.1). */
                  <div
                    className="nav-section-pages"
                    role="group"
                    aria-label={`${section.label} views`}
                  >
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
            /* Same 1.3.1 association as the section groups, but the fallback
             * groups already have a visible heading — point the group at it
             * rather than duplicating the text in an aria-label. Plugin names
             * are unique, so the id is too. */
            <div
              className="nav-group"
              key={group.plugin}
              role="group"
              aria-labelledby={`nav-group-${group.plugin}`}
            >
              <p className="nav-group-heading" id={`nav-group-${group.plugin}`}>
                {group.plugin}
              </p>
              {group.pages.map((p) => (
                <NavLink key={p.key} to={p.to} onClick={closeNav}>
                  {p.label}
                </NavLink>
              ))}
            </div>
          ))}
        </div>
      </nav>
      <main className="shell-main">
        <div className="shell-header">
          <div className="shell-heading">
            <h1>{props.title}</h1>
            {props.subtitle && <p className="shell-subtitle">{props.subtitle}</p>}
          </div>
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
