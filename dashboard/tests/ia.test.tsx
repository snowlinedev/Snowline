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
import type { PluginEntry, UIPage } from "../src/api";
import { SECTIONS, sectionForIntent, sectionNavEntries } from "../src/registry";
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

/** One plugin named `acme` contributing exactly the given pages. */
function acme(pages: UIPage[]): PluginEntry {
  return {
    name: "acme",
    status: "up",
    manifest: {
      name: "acme",
      base_url: "http://127.0.0.1:8899",
      mcp_path: "/mcp",
      health_path: "/health",
      ui_path: null,
      surfaces: {},
      ui: { contract_version: 1, widgets: [], pages },
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
      "system",
    );
    expect(entries.map((e) => e.label)).toEqual([
      "Plugins",
      "Surfaces",
      "Scopes",
      "Ops console",
    ]);
    expect(entries[3].to).toBe("/acme/ops");
  });
});
