import { useEffect, useMemo, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";

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

  // Where the reader is, in nav terms. Tenant links match by PREFIX, so a
  // detail page keeps its parent view marked (/pm/roadmap/item/… → Roadmap) —
  // but two tenants can share a prefix (/pm/roadmap and /pm/roadmap/scopes),
  // and both would then announce aria-current="page". So: the longest tenant
  // route matching the location is THE current one (`currentTo`), and every
  // other tenant link is held to exact matching (`end`), which by
  // construction it then fails. Compared the way NavLink compares — case-
  // insensitively, on a segment boundary — so the two can never disagree.
  const { pathname } = useLocation();
  const here = pathname.toLowerCase().replace(/\/+$/, "") || "/";
  const currentTo = useMemo(() => {
    const routes = [
      ...[...sectionEntries.values()].flat().map((e) => e.to),
      ...navGroups.flatMap((g) => g.pages.map((p) => p.to)),
    ];
    return routes
      .filter((to) => {
        const t = to.toLowerCase();
        return here === t || here.startsWith(`${t}/`);
      })
      .sort((a, b) => b.length - a.length)[0];
  }, [here, sectionEntries, navGroups]);
  const yieldsTo = (to: string) => currentTo !== undefined && currentTo !== to;
  // The current SECTION: the one whose own page this is, or whose tenant is
  // current. app.css shows that section's tenants as the masthead's second
  // row, so the layout follows the same fact aria-current announces.
  const currentSection = SECTIONS.find(
    (s) =>
      here === s.to.toLowerCase() ||
      (sectionEntries.get(s.id) ?? []).some((e) => e.to === currentTo),
  );
  const hasSubnav =
    currentSection !== undefined &&
    (sectionEntries.get(currentSection.id) ?? []).length > 0;

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
      {/* The masthead: brand, the five sections as tabs, and the display
       * preferences, across the top of every page. The nav's DOM is the same
       * tree dashboard-ia.md §3 describes (sections, each with its tenant
       * group, then the fallback plugin groups) — app.css lays it out as a
       * tab row and shows the CURRENT section's tenants as a second row
       * (`nav-section-current` / `masthead-subnav`, set below). */}
      <header
        className={[
          "masthead",
          navOpen && "nav-open",
          hasSubnav && "masthead-subnav",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <div className="masthead-inner">
          <nav className="shell-nav" aria-label="Main">
            <div className="shell-nav-top">
              <div className="brand">
                <span className="brand-mark" aria-hidden="true">
                  <svg viewBox="0 0 24 24" focusable="false">
                    {/* two peaks; everything above the snow line is solid */}
                    <path d="M1.5 20 9.5 5l5 9.4 2-3.4 6 9Z" fill="currentColor" opacity="0.4" />
                    <path d="m9.5 5-3.4 6.4 2.2 1.5 1.5-1.9 1.6 1.7 1.4-1.5Z" fill="currentColor" />
                  </svg>
                </span>
                Snowline
              </div>
              <button
                type="button"
                className="nav-toggle"
                aria-expanded={navOpen}
                aria-controls="shell-nav-links shell-prefs"
                onClick={() => setNavOpen(!navOpen)}
              >
                Menu
              </button>
            </div>
            <div className="shell-nav-links" id="shell-nav-links">
              {SECTIONS.map((section) => {
                const entries = sectionEntries.get(section.id) ?? [];
                return (
                  <div
                    className={
                      section === currentSection
                        ? "nav-section nav-section-current"
                        : "nav-section"
                    }
                    key={section.id}
                  >
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
                          <NavLink key={e.key} to={e.to} end={yieldsTo(e.to)} onClick={closeNav}>
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
                    <NavLink key={p.key} to={p.to} end={yieldsTo(p.to)} onClick={closeNav}>
                      {p.label}
                    </NavLink>
                  ))}
                </div>
              ))}
            </div>
          </nav>
          {/* The display preferences. Below 640px they sit in the opened menu
           * with the links — hence the id the Menu button's aria-controls
           * names alongside the link list. */}
          <div className="toggles" id="shell-prefs">
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
      </header>
      <main className="shell-main">
        <div className="shell-header">
          <h1>{props.title}</h1>
          {props.subtitle && <p className="shell-subtitle">{props.subtitle}</p>}
        </div>
        {props.children}
      </main>
    </div>
  );
}
