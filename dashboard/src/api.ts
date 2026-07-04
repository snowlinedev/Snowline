/** The platform's JSON routes (same origin — the shell is served by the
 * platform at /ui, so these are plain relative fetches through the one trust
 * edge; no CORS anywhere). */

/** A manifest's declarative widget/page contributions (ui-shell.md §3). `kind`
 * is a free string on purpose — an unknown kind registers fine and fails
 * visible at render (§4.4), so the shell never validates it here. */
export type UIWidget = {
  id: string;
  slot: "home";
  kind: string;
  title?: string;
  data: string;
  refresh_seconds?: number;
};

/** `thread` pages may declare a composer (shadow-conversations.md §4): an
 * input-shaped write target, distinct from the button-shaped `actions` §4.3
 * reserves. `endpoint` is plugin-relative, proxied exactly like `data` (§5);
 * `disabled_when` names a `flags` entry (thread GET response) that greys the
 * composer off. */
export type UIComposer = {
  endpoint: string;
  placeholder?: string;
  disabled_when?: string;
};

export type UIPage = {
  id: string;
  route: string;
  title?: string;
  nav: boolean;
  kind: string;
  data: string;
  composer?: UIComposer | null;
};

export type UIBlock = {
  contract_version: number;
  widgets: UIWidget[];
  pages: UIPage[];
};

export type PluginEntry = {
  name: string;
  status: "up" | "down" | "unknown";
  manifest: {
    name: string;
    base_url: string;
    mcp_path: string;
    health_path: string;
    ui_path: string | null;
    surfaces: Record<string, string>;
    ui?: UIBlock | null;
  };
};

export type Surface = {
  name: string;
  route: string;
  allowlist: "*" | string[];
  plugins: string[];
};

export type ScopeNode = {
  slug: string;
  name: string;
  kind: string;
  status: string;
  isolated: boolean;
  children: ScopeNode[];
};

async function get<T>(path: string): Promise<T> {
  const resp = await fetch(path, { headers: { accept: "application/json" } });
  if (!resp.ok) throw new Error(`${path} → ${resp.status}`);
  return (await resp.json()) as T;
}

export const fetchPlugins = () =>
  get<{ plugins: PluginEntry[] }>("/plugins").then((b) => b.plugins);
export const fetchSurfaces = () =>
  get<{ surfaces: Surface[] }>("/surfaces").then((b) => b.surfaces);
export const fetchScopeTree = () =>
  get<{ tree: ScopeNode[] }>("/scopes/tree").then((b) => b.tree);

/** ui-shell.md §5: `GET /ui-api/<plugin>/<path>` proxies to the plugin's own
 * `/ui-api/<path>`. Manifest `data`/widget values are PLUGIN-RELATIVE and
 * always start with `/ui-api/` (enforced at registration —
 * `manifest.py:_valid_ui_data`), so the shell's URL is the platform prefix
 * plus the plugin name plus that path WITH ITS OWN leading `/ui-api` stripped
 * — i.e. `/ui-api/<plugin>` + (data minus the leading `/ui-api`). Example:
 * plugin `governance`, data `/ui-api/widgets/shadow-activity` →
 * `/ui-api/governance/widgets/shadow-activity`. */
export function uiApiUrl(plugin: string, data: string): string {
  const suffix = data.startsWith("/ui-api") ? data.slice("/ui-api".length) : data;
  return `/ui-api/${plugin}${suffix}`;
}

export const fetchUiData = (plugin: string, data: string) =>
  get<unknown>(uiApiUrl(plugin, data));

/** A proxy-POST failure (ui-shell.md §5 / shadow-conversations.md §3):
 * carries the upstream HTTP status alongside the server's `detail` message
 * (FastAPI's standard error shape) so callers can map specific statuses to
 * user-facing copy — 409 archived, 413/422 body-shape, 503 plugin down —
 * without re-parsing the response themselves. */
export class UiApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "UiApiError";
    this.status = status;
  }
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail = `${path} → ${resp.status}`;
    try {
      const data: unknown = await resp.json();
      if (
        data &&
        typeof data === "object" &&
        typeof (data as { detail?: unknown }).detail === "string"
      ) {
        detail = (data as { detail: string }).detail;
      }
    } catch {
      // Non-JSON (or empty) error body — keep the generic status message.
    }
    throw new UiApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

/** The write half of the `/ui-api` proxy (ui-shell.md §5, activated by
 * shadow-conversations.md §3): same plugin-relative -> platform-proxied URL
 * shape as `fetchUiData`, but POSTs a JSON body to a declared write endpoint
 * (today: `composer.endpoint`). Rejects with `UiApiError` on a non-2xx
 * response. */
export const postUiApi = (plugin: string, endpoint: string, body: unknown) =>
  post<unknown>(uiApiUrl(plugin, endpoint), body);
