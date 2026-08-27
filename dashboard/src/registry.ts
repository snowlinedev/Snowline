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

export type SectionNavEntry = { key: string; to: string; label: string };

export type SectionDef = {
  id: SectionId;
  label: string;
  to: string;
  /** Copy for the nothing-landed-here state. A section with nothing to show
   * renders this and NEVER disappears from nav (§3) — a vanishing entry reads
   * as breakage, and the shell's chrome is meant to be stable. (For a section
   * with `native` tenants the copy is unreachable by construction — kept so
   * the table stays uniform and a future de-nativing can't forget it.) */
  empty: string;
  /** Platform-native tenants this section owns unconditionally, ahead of any
   * registered contribution (§3: System absorbs Plugins/Surfaces/Scopes —
   * their routes stay put; System is where they're FOUND). Declared HERE so
   * the table remains the single source both nav and the section pages
   * derive from. Keys lead with ":" — a registered page's key is
   * `<plugin>:<page-id>` with a non-empty plugin name, so a leading colon
   * can never collide with one. */
  native?: readonly SectionNavEntry[];
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
    native: [
      { key: ":plugins", to: "/plugins", label: "Plugins" },
      { key: ":surfaces", to: "/surfaces", label: "Surfaces" },
      { key: ":scopes", to: "/scopes", label: "Scopes" },
    ],
  },
];

/** Look a section up by id. The table is closed (§3: adding a section is a
 * platform PR), so a miss is a programming error, not a data state. */
export function sectionDef(id: SectionId): SectionDef {
  const def = SECTIONS.find((s) => s.id === id);
  if (!def) throw new Error(`unknown section: ${id}`);
  return def;
}

/** Today's bands, in order, each named by the intent it composes (§5). The
 * headings are PLATFORM copy — a plugin declares what a view is for, never
 * what the band is called (§2.2). This table is the source of truth for WHICH
 * intents are Today intents: INTENT_SECTIONS below derives its `today` rows
 * from it, so a Today intent can never be nav-listed with no band to land in
 * — by construction, not by test. */
export const TODAY_BANDS: readonly { intent: string; heading: string }[] = [
  { intent: "attention", heading: "Needs you" },
  { intent: "digest", heading: "Digest" },
  { intent: "activity", heading: "Recent activity" },
];

/** The intent vocabulary this shell version understands (§4.1). A Map, not an
 * object literal, so a contribution declaring `intent: "constructor"` can't
 * inherit a prototype member as its "section". */
export const INTENT_SECTIONS: ReadonlyMap<string, SectionId> = new Map([
  ...TODAY_BANDS.map((b): [string, SectionId] => [b.intent, "today"]),
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

/** The one place a registered page becomes a nav destination: href, label,
 * and React key for every list that links to it (section tenants, fallback
 * groups). Returns null for a `{param}` route — a parameterized page has no
 * single URL to link to, so nav must visibly refuse rather than emit a
 * literal-brace dead link (`/governance/shadow/{branch}`); such pages are
 * reached from row links and should declare `intent: "detail"`, but a
 * mis-declared section intent must degrade to "not listed", never to a
 * broken link. */
export function pageNavEntry(
  plugin: PluginEntry,
  page: UIPage,
): SectionNavEntry | null {
  if (page.route.includes("{")) return null;
  return {
    key: `${plugin.name}:${page.id}`,
    to: `/${plugin.name}${page.route}`,
    label: page.title ?? page.id,
  };
}

/** A section's tenants — the native views it owns (`SectionDef.native`), then
 * every registered linkable page whose `intent` places it here, in
 * plugin-registration order (§4.2). Nav renders these under the section's own
 * link; the section's page lists the same entries (or its empty state), so
 * the two can't disagree.
 *
 * Pages only: widget intents compose Today's bands (`todayBands` below) — a
 * widget has no route to link to, so it is never a nav entry.
 * A page's route is untouched — it stays `/<plugin><route>`; a section is a
 * PLACE in the IA, not a re-namespacing. */
export function sectionNavEntries(
  plugins: PluginEntry[],
  section: SectionDef,
): SectionNavEntry[] {
  const out: SectionNavEntry[] = [...(section.native ?? [])];
  for (const plugin of plugins) {
    for (const page of plugin.manifest.ui?.pages ?? []) {
      if (sectionForIntent(page.intent) !== section.id) continue;
      const entry = pageNavEntry(plugin, page);
      if (entry) out.push(entry);
    }
  }
  return out;
}

export type PluginNavGroup = {
  plugin: string;
  pages: SectionNavEntry[];
};

/** The fallback groups (dashboard-ia.md §3): `nav: true` pages that no intent
 * placed in a section, grouped under a small per-plugin heading (ui-shell.md
 * §6) and rendered AFTER the five sections. Unchanged behavior, demoted from
 * being the whole IA to being the escape hatch — supply-side grouping is
 * where a contribution lands when it hasn't said what it's for. Entries come
 * from the same `pageNavEntry` the sections use, so a `{param}` route can't
 * produce a dead link here either. */
export function pluginNavGroups(plugins: PluginEntry[]): PluginNavGroup[] {
  const groups: PluginNavGroup[] = [];
  for (const plugin of plugins) {
    const pages = (plugin.manifest.ui?.pages ?? [])
      .filter((p) => p.nav && sectionForIntent(p.intent) === "fallback")
      .map((p) => pageNavEntry(plugin, p))
      .filter((e): e is SectionNavEntry => e !== null);
    if (pages.length === 0) continue;
    groups.push({ plugin: plugin.name, pages });
  }
  return groups;
}

export type PluginWidgetEntry = {
  key: string;
  plugin: PluginEntry;
  widget: UIWidget;
};

/** All registered home-slot widgets, in plugin-list order — the order
 * `todayBands` places them in (§4.2: ordering within a band is
 * plugin-registration order). */
export function pluginWidgets(plugins: PluginEntry[]): PluginWidgetEntry[] {
  const out: PluginWidgetEntry[] = [];
  for (const plugin of plugins) {
    for (const widget of plugin.manifest.ui?.widgets ?? []) {
      out.push({ key: `${plugin.name}:${widget.id}`, plugin, widget });
    }
  }
  return out;
}

/* ---- Today's bands (dashboard-ia.md §5) ----------------------------------- */

export type TodayBand = {
  /** Stable React key: the band's intent, or `plugin:<name>` for a fallback
   * band. Intents never contain ":" in the v1 vocabulary, and a plugin band's
   * key is prefixed, so the two families can't collide. */
  key: string;
  heading: string;
  /** Pages placed here by intent — link entries (§5: a page is a destination,
   * not a card the shell can poll into the band). */
  pages: SectionNavEntry[];
  /** Widgets placed here by intent, each rendered as its own self-polling
   * card. */
  widgets: PluginWidgetEntry[];
};

/** Compose Today (§5): intent-declared pages and widgets into the three named
 * bands, then one per-plugin fallback band ("More from <plugin>") for every
 * home widget an intent didn't place. Layout only — nothing is merged, joined,
 * or interpreted (§2.3); the caller renders each widget as its own polling
 * card.
 *
 * A widget whose intent is unknown, absent, or names a section that composes
 * PAGES only (roadmap/features/review/system) lands in the fallback band
 * rather than being dropped: the fail-visible posture (§2.4) means a
 * contribution the shell can't place still shows up where it lives today.
 *
 * Empty bands are dropped here rather than at render, so "a band with no
 * contributions collapses entirely — heading and all" is one decision in one
 * place, and an empty result means "nothing composes into Today" for the
 * caller's empty state. */
export function todayBands(plugins: PluginEntry[]): TodayBand[] {
  const named: TodayBand[] = TODAY_BANDS.map((b) => ({
    key: b.intent,
    heading: b.heading,
    pages: [],
    widgets: [],
  }));
  // A Map, same reason INTENT_SECTIONS is one: an `intent: "constructor"`
  // must not inherit a prototype member as its band.
  const byIntent = new Map(named.map((b) => [b.key, b]));

  for (const plugin of plugins) {
    for (const page of plugin.manifest.ui?.pages ?? []) {
      const band = page.intent == null ? undefined : byIntent.get(page.intent);
      if (!band) continue;
      // Same `pageNavEntry` nav uses, so Today's bands and the Today nav
      // entries can't disagree — including its refusal to link a `{param}`
      // route.
      const entry = pageNavEntry(plugin, page);
      if (entry) band.pages.push(entry);
    }
  }

  const fallbacks = new Map<string, TodayBand>();
  for (const entry of pluginWidgets(plugins)) {
    const intent = entry.widget.intent;
    const band = intent == null ? undefined : byIntent.get(intent);
    if (band) {
      band.widgets.push(entry);
      continue;
    }
    const name = entry.plugin.name;
    let fallback = fallbacks.get(name);
    if (!fallback) {
      fallback = {
        key: `plugin:${name}`,
        heading: `More from ${name}`,
        pages: [],
        widgets: [],
      };
      fallbacks.set(name, fallback);
    }
    fallback.widgets.push(entry);
  }

  return [...named, ...fallbacks.values()].filter(
    (b) => b.pages.length > 0 || b.widgets.length > 0,
  );
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
