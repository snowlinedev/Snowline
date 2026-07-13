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
                // ui-shell.md §5 actions[]: the "New branch" write affordance
                // (issue #123) — the shell renders the button + a minimal form
                // of these declared fields; closed by default, so the axe
                // matrix audits the button and the registered-ui/actions
                // suites drive the form open. No `flags`, so it renders live.
                actions: [
                  {
                    id: "new-branch",
                    label: "New branch",
                    endpoint: "/ui-api/pages/branches",
                    fields: [
                      { name: "scope", label: "Scope", kind: "scope", required: true },
                      { name: "name", label: "Branch name", kind: "text", required: true },
                      {
                        name: "opening_message",
                        label: "Opening note",
                        kind: "multiline",
                        required: false,
                      },
                    ],
                  },
                ],
              },
              {
                id: "shadow-branch",
                route: "/shadow/{branch}",
                title: "Shadow branch",
                nav: false,
                kind: "thread",
                data: "/ui-api/pages/branches/{branch}",
                // shadow-conversations.md §4: the composer declaration — the
                // shared thread fixture below carries no `flags`, so this
                // renders enabled. Also exercises the axe matrix (a11y.test)
                // and the fail-visible/refetch tests (registered-ui.test,
                // composer.test) through the SAME registered page, not a
                // one-off fixture.
                composer: {
                  endpoint: "/ui-api/pages/branches/{branch}/messages",
                  placeholder: "Reply in this branch…",
                  disabled_when: "archived",
                },
              },
              // ui-shell.md §4.2a: the `board` kind — a hierarchical read tree
              // (the pm roadmap shape). >=2 levels of nesting so recursion +
              // the collapse control get exercised; declares group_by + facets
              // so the client-side view toggles render. nav:false but routable
              // at /governance/roadmap, like shadow-branch.
              {
                id: "roadmap",
                route: "/roadmap",
                title: "Roadmap",
                nav: false,
                kind: "board",
                data: "/ui-api/pages/roadmap",
              },
              // Deliberately malformed (a node missing `id`/`label`) — exercises
              // the §4.4 malformed-data card, the board-kind analogue of the
              // broken-stat widget above.
              {
                id: "roadmap-broken",
                route: "/roadmap-broken",
                title: "Broken roadmap",
                nav: false,
                kind: "board",
                data: "/ui-api/pages/roadmap-broken",
              },
              // Malformed `badges`/`facets` (present but wrong-shaped) —
              // validateBoardData must REJECT these, not silently coerce
              // them and crash on a downstream `.map`.
              {
                id: "roadmap-bad-badges",
                route: "/roadmap-bad-badges",
                title: "Roadmap with bad badges",
                nav: false,
                kind: "board",
                data: "/ui-api/pages/roadmap-bad-badges",
              },
              {
                id: "roadmap-bad-facets",
                route: "/roadmap-bad-facets",
                title: "Roadmap with bad facets",
                nav: false,
                kind: "board",
                data: "/ui-api/pages/roadmap-bad-facets",
              },
              // Every top-level node is filtered out by a hidden_by_default
              // facet — the "nothing matches the filters" state, distinct
              // from a truly empty board (props.empty).
              {
                id: "roadmap-all-filtered",
                route: "/roadmap-all-filtered",
                title: "Roadmap, all filtered",
                nav: false,
                kind: "board",
                data: "/ui-api/pages/roadmap-all-filtered",
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
  // ui-shell.md §4.2a board payload: two top-level initiatives (one grouped
  // under snowlinedev, one under acme and flagged `stale`), initiative → phase
  // → item nesting (3 levels), per-node badges/chip/annotation/progress/meta,
  // a collapsed-by-default phase, and a linked leaf item.
  "/ui-api/governance/pages/roadmap": {
    nodes: [
      {
        id: "init-replication",
        label: "Replication continuity",
        kind: "initiative",
        group_key: "snowlinedev",
        chip: "snowlinedev/snowline",
        meta: "3d",
        badges: [{ text: "ACTIVE", intent: "good" }],
        progress: {
          segments: [{ status: "complete" }, { status: "active" }, { status: "upcoming" }],
          complete: 1,
          total: 3,
        },
        facets: { stale: false },
        children: [
          {
            id: "phase-pairing",
            label: "Pairing",
            kind: "phase",
            badges: [{ text: "STUCK", intent: "bad" }],
            annotation: "waiting on the Downgrade flow PR",
            children: [
              {
                id: "item-sign",
                label: "Sign envelopes",
                kind: "item",
                href: "/roadmap/item-sign",
                facets: { initiative_only: false },
              },
              { id: "item-verify", label: "Verify peer", kind: "item" },
            ],
          },
          {
            id: "phase-ingest",
            label: "Ingest",
            kind: "phase",
            collapsed_by_default: true,
            children: [{ id: "item-requeue", label: "Requeue by stream", kind: "item" }],
          },
        ],
      },
      {
        id: "init-stale",
        label: "Stale exploration",
        kind: "initiative",
        group_key: "acme",
        badges: [{ text: "STALE", intent: "neutral" }],
        facets: { stale: true },
        children: [{ id: "phase-idea", label: "Idea", kind: "phase" }],
      },
    ],
    group_by: { key: "group_key", label: "By org", flat_label: "Flat" },
    facets: [
      { key: "stale", label: "Hide stale scopes", hidden_by_default: true },
      { key: "initiative_only", label: "Initiative work only", hidden_by_default: false },
    ],
    empty: "Nothing on the roadmap.",
  },
  // A node missing required `id`/`label` — malformed, fails visible (§4.4).
  "/ui-api/governance/pages/roadmap-broken": {
    nodes: [{ label: "orphan with no id" }],
  },
  // `badges` present but not an array of {text} objects — malformed, fails
  // visible rather than crashing `.map`/being silently dropped.
  "/ui-api/governance/pages/roadmap-bad-badges": {
    nodes: [{ id: "n1", label: "Node", badges: "not-an-array" }],
  },
  // Top-level `facets` present but not an array of {key,label} objects —
  // malformed, fails visible.
  "/ui-api/governance/pages/roadmap-bad-facets": {
    nodes: [{ id: "n1", label: "Node" }],
    facets: "not-an-array",
  },
  // Every top-level node carries a facet a hidden_by_default toggle hides —
  // renders the "nothing matches the filters" state, not a blank page.
  "/ui-api/governance/pages/roadmap-all-filtered": {
    nodes: [{ id: "n1", label: "Filtered node", facets: { stale: true } }],
    facets: [{ key: "stale", label: "Hide stale scopes", hidden_by_default: true }],
    empty: "Nothing on the roadmap.",
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
