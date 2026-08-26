/** Turns registered plugin `ui` blocks (ui-shell.md §3) into shell routing/nav
 * facts: plugin-namespaced routes, grouped nav entries, path-param templating
 * into `data`, and the widget refresh clamp. Kept separate from `kinds.tsx`
 * (the KIND rendering library) because this module is about composition —
 * which routes/nav entries exist — not how a kind's data renders. */

import type { PluginEntry, UIPage, UIWidget } from "./api";

const ROUTE_PARAM = /\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

/** `/shadow/{branch}` -> `/shadow/:branch` (react-router v6 dynamic segment
 * syntax) so a page's declared route can be handed straight to a <Route
 * path=...>. */
export function toRouterPath(route: string): string {
  return route.replace(ROUTE_PARAM, ":$1");
}

/** A page's shell route is namespaced under its plugin (§3): `/shadow` on
 * plugin `governance` mounts at `/governance/shadow`. */
export function pluginRouterPath(pluginName: string, route: UIPage["route"]): string {
  return `/${pluginName}${toRouterPath(route)}`;
}

/** Template a page's `data` path with the router params extracted from its
 * route (path params template into `data` VERBATIM per §3 — e.g. `{branch}`
 * -> the `:branch` router param's value). Values are URI-encoded since they
 * end up in a fetched path. An unresolved param (shouldn't happen — every
 * `{name}` in `data` is expected to appear in the route too) is left as-is
 * rather than silently dropped. */
export function templateData(
  data: string,
  params: Readonly<Record<string, string | undefined>>,
): string {
  return data.replace(ROUTE_PARAM, (whole, name: string) => {
    const value = params[name];
    return value !== undefined ? encodeURIComponent(value) : whole;
  });
}

export type PluginRouteEntry = {
  key: string;
  routerPath: string;
  plugin: PluginEntry;
  page: UIPage;
};

/** Flatten every plugin's registered pages into shell routes, in plugin-list
 * order (stable route identity for React keys). */
export function pluginRoutes(plugins: PluginEntry[]): PluginRouteEntry[] {
  const out: PluginRouteEntry[] = [];
  for (const plugin of plugins) {
    for (const page of plugin.manifest.ui?.pages ?? []) {
      out.push({
        key: `${plugin.name}:${page.id}`,
        routerPath: pluginRouterPath(plugin.name, page.route),
        plugin,
        page,
      });
    }
  }
  return out;
}

/* ---- demand-side sections (dashboard-ia.md §3/§4) ------------------------- */

export type SectionId = "today" | "roadmap" | "features" | "review" | "system";

export type SectionDef = {
  id: SectionId;
  label: string;
  to: string;
  /** Copy for the nothing-landed-here state. A section with nothing to show
   * renders this and NEVER disappears from nav (§3) — a vanishing entry reads
   * as breakage, and the shell's chrome is meant to be stable. */
  empty: string;
};

/** The five sections, in nav order — the owner's questions, not the plugin
 * registry (§2.1). Platform-owned and fixed: adding one is a platform PR
 * revising the spec, the same bar as adding a kind. Both the nav and the
 * routes are generated from this table, so a section can never be listed
 * without resolving (or resolve without being listed). */
export const SECTIONS: readonly SectionDef[] = [
  {
    id: "today",
    label: "Today",
    to: "/",
    empty: "Nothing is composed into Today yet.",
  },
  {
    id: "roadmap",
    label: "Roadmap",
    to: "/roadmap",
    empty: "No roadmap views are registered yet.",
  },
  {
    id: "features",
    label: "Features",
    to: "/features",
    empty: "No feature-status views are registered yet.",
  },
  {
    id: "review",
    label: "Review",
    to: "/review",
    empty: "No review queues are registered yet.",
  },
  {
    id: "system",
    label: "System",
    to: "/system",
    empty: "No system views are registered yet.",
  },
];

/** The intent vocabulary this shell version understands (§4.1). A Map, not an
 * object literal, so a contribution declaring `intent: "constructor"` can't
 * inherit a prototype member as its "section". */
export const INTENT_SECTIONS: ReadonlyMap<string, SectionId> = new Map([
  ["attention", "today"],
  ["digest", "today"],
  ["activity", "today"],
  ["roadmap", "roadmap"],
  ["feature-status", "features"],
  ["review-queue", "review"],
  ["admin", "system"],
] as [string, SectionId][]);

/** Where a declared `intent` places a contribution (§4.1/§4.2):
 *
 * - a known intent -> its section, and `nav` is ignored (the declared purpose
 *   decides placement, never the legacy boolean);
 * - `detail` -> `"none"`: routable but never nav-listed (subsumes `nav:
 *   false`);
 * - an UNKNOWN intent, or none at all -> `"fallback"`: the per-plugin group,
 *   where `nav` keeps its current meaning. Unknown vocabulary degrades to
 *   today's layout and never bricks registration (§2.4/§4.2) — the shell
 *   version owns this table, so a newer plugin against an older shell must
 *   still land somewhere visible.
 *
 * `intent` arrives as `string | null` on the wire (api.ts), so null and
 * undefined are deliberately the same case. */
export function sectionForIntent(
  intent: string | null | undefined,
): SectionId | "fallback" | "none" {
  if (intent == null) return "fallback";
  if (intent === "detail") return "none";
  return INTENT_SECTIONS.get(intent) ?? "fallback";
}

export type SectionNavEntry = { key: string; to: string; label: string };

/** The native views System absorbs (§3). Their routes stay `/plugins`,
 * `/surfaces`, `/scopes` — diagnostics and agent output cite those URLs, so
 * System changes where they're FOUND, not where they live. */
const SYSTEM_NATIVE_PAGES: readonly SectionNavEntry[] = [
  { key: "native:plugins", to: "/plugins", label: "Plugins" },
  { key: "native:surfaces", to: "/surfaces", label: "Surfaces" },
  { key: "native:scopes", to: "/scopes", label: "Scopes" },
];

/** A section's tenants — the native views it owns, then every registered page
 * whose `intent` places it here, in plugin-registration order (§4.2). Nav
 * renders these under the section's own link; the section's page lists the
 * same entries (or its empty state), so the two can't disagree.
 *
 * Pages only: widget intents compose Today's bands, which is step 3 (§5/§8).
 * A page's route is untouched — it stays `/<plugin><route>`; a section is a
 * PLACE in the IA, not a re-namespacing. */
export function sectionNavEntries(
  plugins: PluginEntry[],
  section: SectionId,
): SectionNavEntry[] {
  const out: SectionNavEntry[] = section === "system" ? [...SYSTEM_NATIVE_PAGES] : [];
  for (const plugin of plugins) {
    for (const page of plugin.manifest.ui?.pages ?? []) {
      if (sectionForIntent(page.intent) !== section) continue;
      out.push({
        key: `${plugin.name}:${page.id}`,
        to: `/${plugin.name}${page.route}`,
        label: page.title ?? page.id,
      });
    }
  }
  return out;
}

export type PluginNavGroup = {
  plugin: string;
  pages: { to: string; label: string }[];
};

/** The fallback groups (dashboard-ia.md §3): `nav: true` pages that no intent
 * placed in a section, grouped under a small per-plugin heading (ui-shell.md
 * §6) and rendered AFTER the five sections. Unchanged behavior, demoted from
 * being the whole IA to being the escape hatch — supply-side grouping is
 * where a contribution lands when it hasn't said what it's for. */
export function pluginNavGroups(plugins: PluginEntry[]): PluginNavGroup[] {
  const groups: PluginNavGroup[] = [];
  for (const plugin of plugins) {
    const navPages = (plugin.manifest.ui?.pages ?? []).filter(
      (p) => p.nav && sectionForIntent(p.intent) === "fallback",
    );
    if (navPages.length === 0) continue;
    groups.push({
      plugin: plugin.name,
      pages: navPages.map((p) => ({
        to: `/${plugin.name}${p.route}`,
        label: p.title ?? p.id,
      })),
    });
  }
  return groups;
}

export type PluginWidgetEntry = {
  key: string;
  plugin: PluginEntry;
  widget: UIWidget;
};

/** All registered home-slot widgets, in plugin-list order — the home grid
 * appends these after the native cards (§6). */
export function pluginWidgets(plugins: PluginEntry[]): PluginWidgetEntry[] {
  const out: PluginWidgetEntry[] = [];
  for (const plugin of plugins) {
    for (const widget of plugin.manifest.ui?.widgets ?? []) {
      out.push({ key: `${plugin.name}:${widget.id}`, plugin, widget });
    }
  }
  return out;
}

const MIN_REFRESH_SECONDS = 5;
const DEFAULT_REFRESH_SECONDS = 30;

/** Shell polling clamp (§3: "shell polling hint; shell may clamp"): a floor
 * so a misconfigured plugin can't hammer the proxy, and a default for widgets
 * that don't specify one. */
export function clampRefreshSeconds(requested: number | undefined | null): number {
  if (requested == null || !Number.isFinite(requested)) return DEFAULT_REFRESH_SECONDS;
  return Math.max(MIN_REFRESH_SECONDS, requested);
}

/** A contribution's block-level contract is renderable only at
 * `contract_version === 1` (§3/§4.4) — anything else fails visible per
 * contribution, same as an unknown `kind`. */
export function contractSupported(plugin: PluginEntry): boolean {
  return (plugin.manifest.ui?.contract_version ?? 1) === 1;
}

/** shadow-conversations.md §4: thread pages carrying a composer refetch on a
 * fixed 5s cadence (paused while the tab is hidden) so phase-2 agent replies
 * appear without a manual reload. Composer-less threads are unaffected —
 * this is a page-kind-specific cadence, not a change to the widget
 * `refresh_seconds` clamp above. */
export const THREAD_COMPOSER_POLL_SECONDS = 5;
