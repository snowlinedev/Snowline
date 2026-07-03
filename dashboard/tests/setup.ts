/** Test setup: a deterministic fetch stub for the platform's JSON routes, so
 * page tests render real data shapes without a platform. */
import { vi } from "vitest";

// Node 22+'s experimental `localStorage` global (undefined without
// --localstorage-file) shadows jsdom's implementation under vitest — stub a
// real Storage so prefs.ts behaves as in a browser.
const store = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, String(v)),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
  key: (i: number) => [...store.keys()][i] ?? null,
  get length() {
    return store.size;
  },
});

export const FIXTURES: Record<string, unknown> = {
  "/plugins": {
    plugins: [
      {
        name: "governance",
        status: "up",
        manifest: {
          name: "governance",
          base_url: "http://127.0.0.1:8801",
          mcp_path: "/mcp",
          health_path: "/health",
          ui_path: null,
          surfaces: { "/mcp": "main", "/shadow/mcp": "shadow" },
        },
      },
      {
        name: "pm",
        status: "down",
        manifest: {
          name: "pm",
          base_url: "http://127.0.0.1:8802",
          mcp_path: "/mcp",
          health_path: "/health",
          ui_path: null,
          surfaces: { "/mcp": "main" },
        },
      },
    ],
  },
  "/surfaces": {
    surfaces: [
      { name: "main", route: "/mcp", allowlist: "*", plugins: ["governance", "pm"] },
      { name: "core", route: "/core/mcp", allowlist: ["governance"], plugins: ["governance"] },
    ],
  },
  "/scopes/tree": {
    tree: [
      {
        slug: "snowlinedev",
        name: "SnowlineDev",
        kind: "org",
        status: "active",
        isolated: false,
        children: [
          {
            slug: "snowlinedev/snowline",
            name: "Snowline",
            kind: "project",
            status: "active",
            isolated: false,
            children: [],
          },
        ],
      },
    ],
  },
};

vi.stubGlobal(
  "fetch",
  vi.fn(async (path: string) => {
    const body = FIXTURES[path];
    if (!body) return new Response("not found", { status: 404 });
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }),
);
