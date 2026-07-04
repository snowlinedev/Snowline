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

export type PluginNavGroup = {
  plugin: string;
  pages: { to: string; label: string }[];
};

/** `nav: true` pages, grouped under a small per-plugin heading (§6) — the
 * shell nav is native pages first, then these groups, in plugin-list order. */
export function pluginNavGroups(plugins: PluginEntry[]): PluginNavGroup[] {
  const groups: PluginNavGroup[] = [];
  for (const plugin of plugins) {
    const navPages = (plugin.manifest.ui?.pages ?? []).filter((p) => p.nav);
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
