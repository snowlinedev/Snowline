/** Demand-side IA (docs/specs/dashboard-ia.md §3/§4): the five platform-owned
 * sections, intent -> section composition, the fallback groups that survive as
 * the escape hatch, and the per-route obligations §7 pins (keyboard-reachable
 * section links, current-page marking, document titles).
 *
 * These drive the registry through the real shell rather than a mocked nav:
 * each test declares the plugin registry it needs (`stubRegistry`), so
 * "nothing declares an intent yet" is a first-class fixture, not an accident
 * of the shared one in setup.ts.
 *
 * No jest-dom in this project (see registered-ui.test.tsx) — assertions use
 * plain queries and property/attribute checks. */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { App } from "../src/App";
import type { PluginEntry, UIPage, UIWidget } from "../src/api";
import {
  INTENT_SECTIONS,
  SECTIONS,
  TODAY_BANDS,
  pageNavEntry,
  sectionDef,
  sectionForIntent,
  sectionNavEntries,
  todayBands,
} from "../src/registry";
import { jsonResponse } from "./helpers";
import { FIXTURES } from "./setup";

/** Replace the shared fetch stub with one serving THIS test's registry,
 * falling through to the shared fixtures for everything else (`/surfaces`,
 * `/scopes/tree`, …). `extra` adds `/ui-api` payloads for the declared
 * pages. */
function stubRegistry(plugins: PluginEntry[], extra: Record<string, unknown> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/plugins") return jsonResponse({ plugins });
      const body = extra[path] ?? FIXTURES[path];
      if (!body) return new Response("not found", { status: 404 });
      return jsonResponse(body);
    }),
  );
}

/** One plugin named `acme` contributing exactly the given pages (and, for the
 * Today-band tests, widgets). */
function acme(pages: UIPage[], widgets: UIWidget[] = []): PluginEntry {
  return named("acme", pages, widgets);
}

function named(name: string, pages: UIPage[], widgets: UIWidget[] = []): PluginEntry {
  return {
    name,
    status: "up",
    manifest: {
      name,
      base_url: "http://127.0.0.1:8899",
      mcp_path: "/mcp",
      health_path: "/health",
      ui_path: null,
      surfaces: {},
      ui: { contract_version: 1, widgets, pages },
    },
  };
}

const LIST_PAYLOAD = { items: [{ text: "an item" }] };

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

const mainNav = () => screen.getByRole("navigation", { name: "Main" });

describe("the five sections (§3)", () => {
  it("renders the sections in order, with the native views under System", async () => {
    stubRegistry([]);
    renderAt("/");
    const nav = mainNav();
    await within(nav).findByRole("link", { name: "System" });
    // Sections in spec order, then the native views System absorbed — nothing
    // else, because no plugin declared anything.
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual([
      "Today",
      "Roadmap",
      "Features",
      "Review",
      "System",
      "Plugins",
      "Surfaces",
      "Scopes",
    ]);
  });

  it("keeps the native routes resolving under their cited URLs", async () => {
    // §3: /plugins, /surfaces, /scopes are cited in diagnostics — System is
    // where they're FOUND, not a re-namespacing.
    stubRegistry([]);
    renderAt("/plugins");
    const link = await within(mainNav()).findByRole("link", { name: "Plugins" });
    expect(link.getAttribute("href")).toBe("/plugins");
    expect(link.getAttribute("aria-current")).toBe("page");
  });

  for (const section of SECTIONS) {
    it(`titles the document at ${section.to} (2.4.2)`, async () => {
      stubRegistry([]);
      renderAt(section.to);
      expect(document.title).toBe(`${section.label} · Snowline`);
      // …and the section link marks itself current for assistive tech.
      const link = await within(mainNav()).findByRole("link", { name: section.label });
      expect(link.getAttribute("aria-current")).toBe("page");
    });
  }

  for (const section of SECTIONS.filter((s) => s.id !== "today" && s.id !== "system")) {
    it(`${section.to} renders an explicit empty state when nothing composes into it`, async () => {
      stubRegistry([]);
      renderAt(section.to);
      const note = await screen.findByText(section.empty);
      // Announced, not silent (4.1.3) — and the section stays in nav.
      expect(note.getAttribute("role")).toBe("status");
      expect(within(mainNav()).getByRole("link", { name: section.label })).toBeTruthy();
    });
  }
});

describe("intent -> section composition (§4)", () => {
  const boardPage: UIPage = {
    id: "board",
    route: "/board",
    title: "Portfolio board",
    nav: false, // ignored: a present intent fully determines placement (§4.2)
    kind: "list",
    data: "/ui-api/pages/board",
    intent: "roadmap",
  };

  it("nav-lists a `roadmap` page under Roadmap, not in a plugin fallback group", async () => {
    stubRegistry([acme([boardPage])]);
    renderAt("/");
    const nav = mainNav();
    const link = await within(nav).findByRole("link", { name: "Portfolio board" });
    // Route is untouched — placement is IA, not re-namespacing (§3/§6).
    expect(link.getAttribute("href")).toBe("/acme/board");
    // The fallback group's heading must NOT appear: an intent-placed page is
    // in its section and nowhere else.
    expect(within(nav).queryByText("acme")).toBeNull();
  });

  it("lists a section's tenants on the section page itself", async () => {
    stubRegistry([acme([boardPage])], { "/ui-api/acme/pages/board": LIST_PAYLOAD });
    renderAt("/roadmap");
    const main = screen.getByRole("main");
    const link = await within(main).findByRole("link", { name: "Portfolio board" });
    expect(link.getAttribute("href")).toBe("/acme/board");
    expect(screen.queryByText(SECTIONS[1].empty)).toBeNull();
  });

  it("falls an unknown intent back to the plugin group (§2.4 degrade, never brick)", async () => {
    stubRegistry([
      acme([
        {
          id: "future",
          route: "/future",
          title: "From a newer plugin",
          nav: true,
          kind: "list",
          data: "/ui-api/pages/future",
          intent: "some-intent-this-shell-version-never-heard-of",
        },
      ]),
    ]);
    renderAt("/");
    const nav = mainNav();
    await within(nav).findByText("acme"); // the fallback group's heading
    const link = within(nav).getByRole("link", { name: "From a newer plugin" });
    expect(link.getAttribute("href")).toBe("/acme/future");
  });

  const detailPage: UIPage = {
    id: "thing",
    route: "/thing",
    title: "One thing",
    nav: true, // ignored: `detail` subsumes nav: false (§4.2)
    kind: "list",
    data: "/ui-api/pages/thing",
    intent: "detail",
  };

  it("gives an `intent: detail` page no nav entry", async () => {
    stubRegistry([acme([detailPage])]);
    renderAt("/");
    const nav = mainNav();
    await within(nav).findByRole("link", { name: "System" });
    expect(within(nav).queryByRole("link", { name: "One thing" })).toBeNull();
    expect(within(nav).queryByText("acme")).toBeNull();
  });

  it("still resolves an `intent: detail` page's route (§6 permalinks)", async () => {
    stubRegistry([acme([detailPage])], { "/ui-api/acme/pages/thing": LIST_PAYLOAD });
    renderAt("/acme/thing");
    await screen.findByRole("heading", { level: 1, name: "One thing" });
    expect(screen.queryByText("No such page.")).toBeNull();
  });

  it("treats `intent: null` and an absent intent identically", async () => {
    // `intent` arrives as `string | null` on the wire (api.ts) — an explicit
    // null must not be mistaken for a declared (unknown) value.
    stubRegistry([
      acme([
        {
          id: "explicit-null",
          route: "/explicit-null",
          title: "Explicit null",
          nav: true,
          kind: "list",
          data: "/ui-api/pages/explicit-null",
          intent: null,
        },
        {
          id: "absent",
          route: "/absent",
          title: "Absent",
          nav: true,
          kind: "list",
          data: "/ui-api/pages/absent",
        },
      ]),
    ]);
    renderAt("/");
    const nav = mainNav();
    const group = await within(nav).findByText("acme");
    expect(group.textContent).toBe("acme");
    // Both land in the same fallback group, in registration order.
    expect(within(nav).getByRole("link", { name: "Explicit null" })).toBeTruthy();
    expect(within(nav).getByRole("link", { name: "Absent" })).toBeTruthy();
  });
});

describe("sectionForIntent (§4.1 vocabulary)", () => {
  it("maps every v1 intent to its section", () => {
    expect(sectionForIntent("attention")).toBe("today");
    expect(sectionForIntent("digest")).toBe("today");
    expect(sectionForIntent("activity")).toBe("today");
    expect(sectionForIntent("roadmap")).toBe("roadmap");
    expect(sectionForIntent("feature-status")).toBe("features");
    expect(sectionForIntent("review-queue")).toBe("review");
    expect(sectionForIntent("admin")).toBe("system");
  });

  it("routes `detail` to no nav entry at all", () => {
    expect(sectionForIntent("detail")).toBe("none");
  });

  it("degrades an unknown intent — and null/undefined — to the fallback group", () => {
    expect(sectionForIntent("chart-of-the-future")).toBe("fallback");
    expect(sectionForIntent("")).toBe("fallback");
    expect(sectionForIntent(null)).toBe("fallback");
    expect(sectionForIntent(undefined)).toBe("fallback");
    // Not a section just because Object.prototype has the member.
    expect(sectionForIntent("constructor")).toBe("fallback");
    expect(sectionForIntent("toString")).toBe("fallback");
  });

  it("places an `admin` page after the native views System already owns", () => {
    const entries = sectionNavEntries(
      [
        acme([
          {
            id: "ops",
            route: "/ops",
            title: "Ops console",
            nav: false,
            kind: "list",
            data: "/ui-api/pages/ops",
            intent: "admin",
          },
        ]),
      ],
      sectionDef("system"),
    );
    expect(entries.map((e) => e.label)).toEqual([
      "Plugins",
      "Surfaces",
      "Scopes",
      "Ops console",
    ]);
    expect(entries[3].to).toBe("/acme/ops");
  });

  it("cannot collide a plugin's page key with a native tenant key", () => {
    // Native keys lead with ":" — `pageNavEntry` keys are
    // `<plugin>:<page-id>` with a non-empty plugin name, so even a plugin
    // named to imitate the old "native:" pseudo-namespace yields unique keys.
    const nativePlugin: PluginEntry = {
      ...acme([
        {
          id: "plugins",
          route: "/plugins-view",
          title: "Impostor",
          nav: false,
          kind: "list",
          data: "/ui-api/pages/plugins-view",
          intent: "admin",
        },
      ]),
      name: "native",
    };
    nativePlugin.manifest = { ...nativePlugin.manifest, name: "native" };
    const keys = sectionNavEntries([nativePlugin], sectionDef("system")).map(
      (e) => e.key,
    );
    expect(new Set(keys).size).toBe(keys.length);
  });
});

describe("nav link hygiene", () => {
  it("refuses a nav entry for a `{param}` route instead of emitting a dead link", async () => {
    // A parameterized page has no single URL — a mis-declared section intent
    // must degrade to "not listed" (reached by row links, like `detail`),
    // never to a literal-brace href that fetches "%7Bbranch%7D".
    const paramPage: UIPage = {
      id: "shadow-branch",
      route: "/shadow/{branch}",
      title: "Shadow branch",
      nav: true,
      kind: "thread",
      data: "/ui-api/pages/branches/{branch}",
      intent: "review-queue",
    };
    expect(pageNavEntry(acme([paramPage]), paramPage)).toBeNull();
    stubRegistry([acme([paramPage])]);
    renderAt("/review");
    // Neither in nav nor on the section page — and the section says so.
    await screen.findByText(sectionDef("review").empty);
    expect(screen.queryByRole("link", { name: "Shadow branch" })).toBeNull();
  });

  it("marks only the exact section current — a deeper unknown URL marks nothing", async () => {
    // Sections have no child routes: /roadmap/anything is the catch-all's
    // "No such page.", and prefix-matching must not leave Roadmap announcing
    // aria-current="page" on it.
    stubRegistry([]);
    renderAt("/roadmap/stale-cited-url");
    await screen.findByText("No such page.");
    const link = within(mainNav()).getByRole("link", { name: "Roadmap" });
    expect(link.getAttribute("aria-current")).toBeNull();
  });

  describe("tenants that share a route prefix", () => {
    // pm's real shape: /roadmap (the board) and /roadmap/scopes (the directory)
    // are both Roadmap tenants, and the first is a prefix of the second.
    const boardPage: UIPage = {
      id: "board",
      route: "/board",
      title: "Board",
      nav: true,
      kind: "list",
      data: "/ui-api/pages/board",
      intent: "roadmap",
    };
    const scopesPage: UIPage = {
      id: "scopes",
      route: "/board/scopes",
      title: "By scope",
      nav: true,
      kind: "list",
      data: "/ui-api/pages/scopes",
      intent: "roadmap",
    };
    const currentLinks = () =>
      within(mainNav())
        .getAllByRole("link")
        .filter((a) => a.getAttribute("aria-current") === "page")
        .map((a) => a.textContent);

    it("marks only the longest match current, not every prefix of it", async () => {
      stubRegistry([acme([boardPage, scopesPage])], {
        "/ui-api/acme/pages/scopes": LIST_PAYLOAD,
      });
      renderAt("/acme/board/scopes");
      // screen, not a held nav: the route resolves once plugins load, which
      // remounts the shell — currentLinks() then reads the live nav.
      await screen.findByRole("link", { name: "By scope" });
      expect(currentLinks()).toEqual(["By scope"]);
    });

    it("still marks a tenant current on its own detail pages", async () => {
      // A deeper URL under the board that is NOT the sibling tenant keeps the
      // board marked — prefix matching is what tells a detail page's reader
      // which view they are in.
      stubRegistry([acme([boardPage, scopesPage])]);
      renderAt("/acme/board/item/123");
      await within(mainNav()).findByRole("link", { name: "Board" });
      expect(currentLinks()).toEqual(["Board"]);
    });
  });

  it("lists a today-intent page under Today in nav AND on the Today page itself", async () => {
    // The §3 nav↔page invariant holds for Today like any section: an
    // attention/digest/activity page nav-lists under Today, so `/` must
    // surface it too — as a link entry inside its own band (§5).
    const digestPage: UIPage = {
      id: "digest",
      route: "/digest",
      title: "Morning digest",
      nav: false,
      kind: "list",
      data: "/ui-api/pages/digest",
      intent: "digest",
    };
    stubRegistry([acme([digestPage])]);
    renderAt("/");
    const nav = mainNav();
    const navLink = await within(nav).findByRole("link", { name: "Morning digest" });
    expect(navLink.getAttribute("href")).toBe("/acme/digest");
    const main = screen.getByRole("main");
    const pageLink = await within(main).findByRole("link", { name: "Morning digest" });
    expect(pageLink.getAttribute("href")).toBe("/acme/digest");
  });

  it("associates tenants with their section for assistive tech (1.3.1)", async () => {
    stubRegistry([
      acme([
        {
          id: "board",
          route: "/board",
          title: "Portfolio board",
          nav: false,
          kind: "list",
          data: "/ui-api/pages/board",
          intent: "roadmap",
        },
      ]),
    ]);
    renderAt("/");
    const nav = mainNav();
    const group = await within(nav).findByRole("group", { name: "Roadmap views" });
    expect(within(group).getByRole("link", { name: "Portfolio board" })).toBeTruthy();
    // The native System tenants get the same association.
    const system = within(nav).getByRole("group", { name: "System views" });
    expect(within(system).getByRole("link", { name: "Plugins" })).toBeTruthy();
  });
});

describe("Today: banded composition (§5)", () => {
  /** A home widget of the `stat` kind, optionally declaring an intent. */
  function statWidget(id: string, intent?: string | null): UIWidget {
    return {
      id,
      slot: "home",
      kind: "stat",
      title: `Widget ${id}`,
      data: `/ui-api/widgets/${id}`,
      ...(intent === undefined ? {} : { intent }),
    };
  }

  const statPayload = (id: string, plugin = "acme") => ({
    [`/ui-api/${plugin}/widgets/${id}`]: { value: 1, label: id },
  });

  /** The band headings, in rendered order — h2 is the band level (card titles
   * inside a band are h3), so this reads the page's band outline exactly. */
  const bandHeadings = () =>
    within(screen.getByRole("main"))
      .queryAllByRole("heading", { level: 2 })
      .map((h) => h.textContent);

  it("covers every Today intent with a band", () => {
    // Held by construction — INTENT_SECTIONS derives its `today` rows from
    // TODAY_BANDS — so a `today` intent can't nav-list with no band to land
    // in. This pin guards the derivation itself (someone re-inlining the
    // rows), not a second table.
    const todayIntents = [...INTENT_SECTIONS]
      .filter(([, section]) => section === "today")
      .map(([intent]) => intent);
    expect(TODAY_BANDS.map((b) => b.intent)).toEqual(todayIntents);
  });

  it("renders the bands in spec order, and only the populated ones", async () => {
    // attention + activity contribute; digest doesn't — so Digest collapses
    // entirely, heading and all, and the surviving two keep spec order.
    stubRegistry(
      [
        acme(
          [
            {
              id: "shipped",
              route: "/shipped",
              title: "What shipped",
              nav: false,
              kind: "list",
              data: "/ui-api/pages/shipped",
              intent: "activity",
            },
          ],
          [statWidget("blocked", "attention")],
        ),
      ],
      statPayload("blocked"),
    );
    renderAt("/");
    await screen.findByText("Needs you");
    expect(bandHeadings()).toEqual(["Needs you", "Recent activity"]);
    expect(screen.queryByText("Digest")).toBeNull();
  });

  it("renders a digest-intent widget's card inside the Digest band", async () => {
    stubRegistry(
      [acme([], [statWidget("since-you-looked", "digest")])],
      statPayload("since-you-looked"),
    );
    renderAt("/");
    // The band is a labelled region, so the card is findable BY BAND — the
    // placement, not just the presence, is what §5 promises.
    const band = await screen.findByRole("region", { name: "Digest" });
    expect(
      within(band).getByRole("heading", { level: 3, name: "Widget since-you-looked" }),
    ).toBeTruthy();
    // …and it rendered its own polled data, not a merged summary.
    expect(within(band).getByText("since-you-looked")).toBeTruthy();
    expect(bandHeadings()).toEqual(["Digest"]);
  });

  it("lands an intent-less home widget in its plugin's fallback band", async () => {
    // §4.2: `slot: "home"` with no intent keeps working — demoted to the
    // per-plugin fallback band, never dropped.
    stubRegistry([acme([], [statWidget("legacy")])], statPayload("legacy"));
    renderAt("/");
    const band = await screen.findByRole("region", { name: "More from acme" });
    expect(within(band).getByRole("heading", { level: 3, name: "Widget legacy" })).toBeTruthy();
    expect(bandHeadings()).toEqual(["More from acme"]);
  });

  it("fails a page-kind widget visible instead of rendering it (§4.1)", async () => {
    // Widget kinds are stat/list; a widget declaring a PAGE kind (board)
    // must hit the UnsupportedKindCard, not silently render — a board
    // widget would also mint a second owner of the route's query string
    // (§4.2a's one-board-per-route premise).
    stubRegistry([
      acme(
        [],
        [
          {
            id: "sneaky",
            slot: "home",
            kind: "board",
            title: "Sneaky board",
            data: "/ui-api/widgets/sneaky",
          },
        ],
      ),
    ]);
    renderAt("/");
    await screen.findByText(
      "acme offers a view this platform version can't render (kind 'board')",
    );
  });

  it("rejects an empty facet key as malformed board data", async () => {
    // An empty key would round-trip through ?show=/?hide= as a bare param
    // the read side strips — a permanently stuck toggle. Malformed, loudly.
    stubRegistry(
      [
        acme([
          {
            id: "b",
            route: "/b",
            title: "B",
            nav: false,
            kind: "board",
            data: "/ui-api/pages/b",
            intent: "roadmap",
          },
        ]),
      ],
      {
        "/ui-api/acme/pages/b": {
          nodes: [{ id: "n1", label: "Node" }],
          facets: [{ key: "", label: "Hide drafts", hidden_by_default: true }],
        },
      },
    );
    renderAt("/acme/b");
    const error = await screen.findByRole("alert");
    expect(error.textContent).toContain("malformed data");
  });

  it("banks a today-intent page as a link inside its band", async () => {
    stubRegistry([
      acme([
        {
          id: "needs-you",
          route: "/needs-you",
          title: "Needs your judgment",
          nav: false,
          kind: "list",
          data: "/ui-api/pages/needs-you",
          intent: "attention",
        },
      ]),
    ]);
    renderAt("/");
    const band = await screen.findByRole("region", { name: "Needs you" });
    const link = within(band).getByRole("link", { name: "Needs your judgment" });
    expect(link.getAttribute("href")).toBe("/acme/needs-you");
  });

  it("renders an explicit empty state when nothing composes into Today", async () => {
    stubRegistry([]);
    renderAt("/");
    const note = await screen.findByText(sectionDef("today").empty);
    expect(note.getAttribute("role")).toBe("status");
    expect(bandHeadings()).toEqual([]);
    // …and Today stays in nav (§3: a section never vanishes).
    expect(within(mainNav()).getByRole("link", { name: "Today" })).toBeTruthy();
  });

  it("orders bands' contributions by plugin registration (§4.2)", () => {
    // Unit-level: two plugins both contributing to one band, plus each one's
    // own fallback band, in registration order.
    const bands = todayBands([
      named("first", [], [statWidget("a", "attention"), statWidget("legacy-a")]),
      named("second", [], [statWidget("b", "attention"), statWidget("legacy-b")]),
    ]);
    expect(bands.map((b) => b.heading)).toEqual([
      "Needs you",
      "More from first",
      "More from second",
    ]);
    expect(bands[0].widgets.map((w) => w.key)).toEqual(["first:a", "second:b"]);
    // Unknown intent converges with null intent BEFORE render — same fallback
    // band (§2.4 degrade, never drop); the rendered outcome is covered once by
    // the intent-less test above.
    const unknown = todayBands([
      named("first", [], [statWidget("future", "an-intent-from-a-newer-plugin")]),
    ]);
    expect(unknown.map((b) => b.heading)).toEqual(["More from first"]);
    expect(unknown[0].widgets.map((w) => w.key)).toEqual(["first:future"]);
  });

  it("does not render the machine's health cards on Today (§3)", async () => {
    // The native cards moved to System — Today is the owner's work.
    stubRegistry([acme([], [statWidget("legacy")])], statPayload("legacy"));
    renderAt("/");
    await screen.findByText("More from acme");
    const main = screen.getByRole("main");
    expect(within(main).queryByText("Plugins up")).toBeNull();
    expect(within(main).queryByText("Plugin status")).toBeNull();
  });
});

describe("System hosts the native health cards (§3)", () => {
  it("renders them above the section's tenant links", async () => {
    stubRegistry([acme([], [])]);
    renderAt("/system");
    const main = screen.getByRole("main");
    // By heading, not by text: "Surfaces" is also a tenant LINK on this page,
    // and the card titles are what this test is about.
    await within(main).findByRole("heading", { level: 2, name: "Plugins up" });
    expect(
      within(main).getByRole("heading", { level: 2, name: "Plugin status" }),
    ).toBeTruthy();
    expect(within(main).getByRole("heading", { level: 2, name: "Surfaces" })).toBeTruthy();
    // The plugins-up stat reads the shared /plugins result (1 registered, up).
    expect(within(main).getByText("1 / 1")).toBeTruthy();
    // Surfaces come from the shared fixture's own /surfaces fetch.
    await within(main).findByRole("link", { name: "main" });
    // …and the native tenant links still follow.
    const links = within(main).getAllByRole("link").map((a) => a.textContent);
    expect(links).toContain("Plugins");
    expect(links).toContain("Scopes");
    expect(links.indexOf("Plugins")).toBeGreaterThan(links.indexOf("main"));
  });
});
