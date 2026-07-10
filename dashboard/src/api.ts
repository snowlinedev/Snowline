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

/** A declared form field of a page `action` (ui-shell.md §5): the shell
 * renders one labelled control per field and submits `{ name: value }` in the
 * action's POST body. `kind` is a rendering hint — `text` (default),
 * `multiline`, or `scope` (a text input with a `<datalist>` typeahead over the
 * platform's scope slugs); an unknown value falls back to a text control. */
export type UIActionField = {
  name: string;
  label?: string;
  kind?: string;
  required?: boolean;
};

/** A page-level write affordance (ui-shell.md §5 actions[]): a labelled button
 * that opens a minimal form of `fields` and POSTs their values through the
 * `/ui-api` proxy to `endpoint` (allowlisted exactly like `composer`). On a
 * 2xx the shell follows an optional plugin-relative `navigate` href in the
 * response. Rendered GENERICALLY — the shell knows nothing plugin-specific. */
export type UIAction = {
  id: string;
  label: string;
  endpoint: string;
  fields?: UIActionField[];
};

export type UIPage = {
  id: string;
  route: string;
  title?: string;
  nav: boolean;
  kind: string;
  data: string;
  composer?: UIComposer | null;
  actions?: UIAction[] | null;
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

/** Every scope slug in the tree, flattened depth-first and sorted — the source
 * for the `scope` action-field kind's `<datalist>` typeahead (ui-shell.md
 * §5.1). Reuses the Scopes page's `/scopes/tree` data path rather than a new
 * endpoint. */
export function flattenScopeSlugs(tree: ScopeNode[]): string[] {
  const out: string[] = [];
  const walk = (nodes: ScopeNode[]) => {
    for (const n of nodes) {
      out.push(n.slug);
      walk(n.children);
    }
  };
  walk(tree);
  return out.sort();
}

export const fetchScopeSlugs = (): Promise<string[]> =>
  fetchScopeTree().then(flattenScopeSlugs);

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
  // A 2xx IS success — callers of the write path don't consume the body, so a
  // 204/empty/non-JSON success reply must not surface as a false failure
  // (which would strand the draft and bait a duplicate re-send of a message
  // the server already persisted).
  try {
    return (await resp.json()) as T;
  } catch {
    return undefined as T;
  }
}

/** The write half of the `/ui-api` proxy (ui-shell.md §5, activated by
 * shadow-conversations.md §3): same plugin-relative -> platform-proxied URL
 * shape as `fetchUiData`, but POSTs a JSON body to a declared write endpoint
 * (today: `composer.endpoint`). Rejects with `UiApiError` on a non-2xx
 * response. */
export const postUiApi = (plugin: string, endpoint: string, body: unknown) =>
  post<unknown>(uiApiUrl(plugin, endpoint), body);
