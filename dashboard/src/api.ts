/** The platform's JSON routes (same origin — the shell is served by the
 * platform at /ui, so these are plain relative fetches through the one trust
 * edge; no CORS anywhere). */

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
