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
          // ui-shell.md §3: one home widget + a table page (nav) + a thread
          // page (row-linked, not nav) — plus a deliberately-broken widget
          // and a deliberately-unknown kind, for the fail-visible tests.
          ui: {
            contract_version: 1,
            widgets: [
              {
                id: "shadow-activity",
                slot: "home",
                kind: "stat",
                title: "Open shadow branches",
                data: "/ui-api/widgets/shadow-activity",
                refresh_seconds: 30,
              },
              {
                id: "broken-stat",
                slot: "home",
                kind: "stat",
                title: "Broken stat",
                data: "/ui-api/widgets/broken-stat",
              },
              {
                id: "chart-widget",
                slot: "home",
                kind: "chart",
                title: "Unsupported widget",
                data: "/ui-api/widgets/chart",
              },
            ],
            pages: [
              {
                id: "shadow-branches",
                route: "/shadow",
                title: "Shadow discussions",
                nav: true,
                kind: "table",
                data: "/ui-api/pages/branches",
              },
              {
                id: "shadow-branch",
                route: "/shadow/{branch}",
                title: "Shadow branch",
                nav: false,
                kind: "thread",
                data: "/ui-api/pages/branches/{branch}",
              },
            ],
          },
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
  "/ui-api/governance/widgets/shadow-activity": {
    value: 3,
    label: "open branches",
  },
  // Deliberately malformed (missing required `value`) — exercises the §4.4
  // malformed-data error card.
  "/ui-api/governance/widgets/broken-stat": { nope: true },
  "/ui-api/governance/pages/branches": {
    columns: [
      { key: "branch", label: "Branch" },
      { key: "status", label: "Status" },
    ],
    rows: [
      {
        cells: { branch: "main-plan-x", status: "open" },
        href: "/shadow/main-plan-x",
      },
    ],
    empty: "No branches.",
  },
  "/ui-api/governance/pages/branches/main-plan-x": {
    title: "main-plan-x",
    meta: "Status: open",
    nodes: [
      {
        author: "sean",
        kind: "comment",
        markdown:
          "Discussion about **main-plan-x**.\n\n<script>alert(1)</script> should stay text.",
        at: "2026-07-01T12:00:00Z",
        citations: ["decision-abc"],
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
