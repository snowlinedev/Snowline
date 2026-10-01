/** The `board` kind (ui-shell.md §4.2a): a hierarchical, collapsible,
 * read-only tree with client-side group-by / facet toggles applied to one
 * already-fetched payload — no refetch, and the toggle state carried by the
 * route's query string rather than stored anywhere. Driven through the
 * registered `/governance/roadmap` page (fixture in tests/setup.ts) so it
 * renders through the exact same kind dispatch as everything else.
 *
 * No jest-dom in this project (see the rest of the suite) — assertions use
 * plain queries (`getByText`/`getByRole` throw if absent) and attribute checks
 * rather than `toBeInTheDocument()`. `hidden` children are queried on the DOM
 * node's `.hidden` property, the state the collapse control toggles. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";

/** The board's view state lives in the route's query string (§4.2a), so the
 * tests need to READ the current URL — a probe rendered inside the same
 * MemoryRouter is the observable. */
function LocationProbe() {
  const loc = useLocation();
  return <span data-testid="url">{loc.pathname + loc.search}</span>;
}
function currentUrl(): string {
  return screen.getByTestId("url").textContent ?? "";
}

/** A real Back, for the "toggles replace rather than push" test. */
function BackButton() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(-1)}>
      GO BACK
    </button>
  );
}

function renderRoadmap(entry = "/governance/roadmap") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <App />
      <LocationProbe />
    </MemoryRouter>,
  );
}

/* The drawer (§4.2a): the board root carries the docked-vs-overlay LAYOUT class
 * and the panel is a `hidden`-toggled region, so — as with the collapse control
 * — the DOM node's own `.hidden`/`className` is the state under test, not a
 * visibility heuristic. Everything a USER can reach is queried by role instead,
 * which excludes the `hidden` panel from the a11y tree for free. */
function boardRoot(): HTMLElement {
  return document.querySelector(".board") as HTMLElement;
}
function drawerPanel(): HTMLElement {
  return document.querySelector(".board-drawer") as HTMLElement;
}
/** The drawer toggle, by its composed label: title + visible count (+ the
 * plugin's `count_label`). `stale` is hidden_by_default, so 2 of the fixture's
 * 3 queue nodes are visible on arrival. */
function drawerToggle(count = 2): HTMLElement {
  return screen.getByRole("button", { name: `Placement queue · ${count} waiting` });
}

describe("board kind: rendering", () => {
  it("renders nested nodes, badges, chip, annotation, progress and links", async () => {
    renderRoadmap();
    await screen.findByText("Replication continuity");
    // Nested phase + item (3 levels of recursion).
    expect(screen.getByText("Pairing")).toBeTruthy();
    expect(screen.getByText("Sign envelopes")).toBeTruthy();
    // A badge is visible TEXT (never color-only).
    expect(screen.getByText("STUCK")).toBeTruthy();
    // Chip, annotation, meta, progress count all render.
    expect(screen.getByText("snowlinedev/snowline")).toBeTruthy();
    expect(screen.getByText("waiting on the Downgrade flow PR")).toBeTruthy();
    expect(screen.getByText("1/3")).toBeTruthy();
    // A node `href` is plugin-relative in the fixture ("/roadmap/item-sign")
    // and re-namespaced under the plugin (ui-shell.md §3 href rule).
    const link = screen.getByRole("link", { name: "Sign envelopes" });
    expect(link.getAttribute("href")).toBe("/governance/roadmap/item-sign");
  });

  it("starts with hidden_by_default facets filtering matching nodes out", async () => {
    renderRoadmap();
    await screen.findByText("Replication continuity");
    // `stale` is hidden_by_default, so the stale initiative is filtered out.
    expect(screen.queryByText("Stale exploration")).toBeNull();
  });

  it("fails visible on a malformed board payload", async () => {
    render(
      <MemoryRouter initialEntries={["/governance/roadmap-broken"]}>
        <App />
      </MemoryRouter>,
    );
    const error = await screen.findByRole("alert");
    expect(error.textContent).toContain("governance");
    // The card names the plugin-relative data path (as the broken-stat widget
    // does), not the proxied /ui-api/<plugin>/… URL.
    expect(error.textContent).toContain("/ui-api/pages/roadmap-broken");
  });

  it("fails visible on wrong-shaped badges rather than crashing", async () => {
    render(
      <MemoryRouter initialEntries={["/governance/roadmap-bad-badges"]}>
        <App />
      </MemoryRouter>,
    );
    const error = await screen.findByRole("alert");
    expect(error.textContent).toContain("/ui-api/pages/roadmap-bad-badges");
  });

  it("fails visible on wrong-shaped top-level facets rather than crashing", async () => {
    render(
      <MemoryRouter initialEntries={["/governance/roadmap-bad-facets"]}>
        <App />
      </MemoryRouter>,
    );
    const error = await screen.findByRole("alert");
    expect(error.textContent).toContain("/ui-api/pages/roadmap-bad-facets");
  });

  it("shows a filtered-empty state, not a blank page, when every node is hidden", async () => {
    render(
      <MemoryRouter initialEntries={["/governance/roadmap-all-filtered"]}>
        <App />
      </MemoryRouter>,
    );
    await screen.findByRole("button", { name: "Hide stale scopes" });
    expect(screen.queryByText("Filtered node")).toBeNull();
    expect(screen.getByText("Nothing matches the current filters.")).toBeTruthy();
    // Distinct from the plugin's true-empty `empty` copy, which never renders
    // here — nodes DO exist, they're just all filtered.
    expect(screen.queryByText("Nothing on the roadmap.")).toBeNull();
  });
});

describe("board kind: collapse control", () => {
  it("a collapsed-by-default node hides its subtree until expanded", async () => {
    const user = userEvent.setup();
    renderRoadmap();
    await screen.findByText("Replication continuity");
    // "Ingest" is collapsed_by_default: its child list is present but `hidden`.
    const requeue = screen.getByText("Requeue by stream");
    const ingestList = requeue.closest("ul.board-children") as HTMLElement;
    expect(ingestList.hidden).toBe(true);

    const expand = screen.getByRole("button", { name: "Expand Ingest" });
    expect(expand.getAttribute("aria-expanded")).toBe("false");
    await user.click(expand);
    expect(ingestList.hidden).toBe(false);
    expect(
      screen.getByRole("button", { name: "Collapse Ingest" }).getAttribute("aria-expanded"),
    ).toBe("true");
  });

  it("collapsing an expanded node hides its children", async () => {
    const user = userEvent.setup();
    renderRoadmap();
    await screen.findByText("Pairing");
    const sign = screen.getByText("Sign envelopes");
    const pairingList = sign.closest("ul.board-children") as HTMLElement;
    expect(pairingList.hidden).toBe(false);
    await user.click(screen.getByRole("button", { name: "Collapse Pairing" }));
    expect(pairingList.hidden).toBe(true);
  });
});

describe("board kind: facet toggle", () => {
  it("un-hiding the stale facet reveals the filtered node without refetch", async () => {
    const user = userEvent.setup();
    renderRoadmap();
    await screen.findByText("Replication continuity");
    const hideStale = screen.getByRole("button", { name: "Hide stale scopes" });
    // hidden_by_default => the toggle starts pressed (actively hiding).
    expect(hideStale.getAttribute("aria-pressed")).toBe("true");
    await user.click(hideStale);
    expect(hideStale.getAttribute("aria-pressed")).toBe("false");
    // The formerly-hidden initiative is now visible — no network involved.
    await screen.findByText("Stale exploration");
  });
});

describe("board kind: group-by toggle", () => {
  it("switches from flat to grouped and back, bucketing by group_key", async () => {
    const user = userEvent.setup();
    renderRoadmap();
    await screen.findByText("Replication continuity");
    const flat = screen.getByRole("button", { name: "Flat" });
    const byOrg = screen.getByRole("button", { name: "By org" });
    // Flat selected by default (§4.2a).
    expect(flat.getAttribute("aria-pressed")).toBe("true");
    expect(byOrg.getAttribute("aria-pressed")).toBe("false");
    // No group heading in flat view.
    expect(screen.queryByRole("heading", { name: "snowlinedev" })).toBeNull();

    await user.click(byOrg);
    expect(byOrg.getAttribute("aria-pressed")).toBe("true");
    // Grouped: a heading per group_key bucket (only snowlinedev visible while
    // the stale acme initiative is still filtered out).
    await screen.findByRole("heading", { name: "snowlinedev" });
    // Same node still present under its group — grouping never drops nodes.
    expect(screen.getByText("Replication continuity")).toBeTruthy();

    await user.click(flat);
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "snowlinedev" })).toBeNull(),
    );
  });
});

/** ui-shell.md §4.2a: the board's view state lives in the route's query string
 * (`?show=<facet>` / `?hide=<facet>` / `?group=1`, non-defaults only), so the
 * filters survive a refresh, a board URL deep-links a view, and toggling
 * replaces rather than pushes history. */
describe("board kind: view state in the URL", () => {
  it("a show= param pre-applies the filter and its toggle reflects it", async () => {
    renderRoadmap("/governance/roadmap?show=stale");
    await screen.findByText("Replication continuity");
    // `stale` is hidden_by_default; ?show=stale un-hides it before any click.
    expect(screen.getByText("Stale exploration")).toBeTruthy();
    expect(
      screen
        .getByRole("button", { name: "Hide stale scopes" })
        .getAttribute("aria-pressed"),
    ).toBe("false");
  });

  it("a hide= param filters a default-visible facet's nodes out", async () => {
    renderRoadmap("/governance/roadmap?hide=initiative_only");
    await screen.findByText("Replication continuity");
    // "Verify peer" carries `initiative_only`, so the forced-on facet drops it.
    expect(screen.queryByText("Verify peer")).toBeNull();
    expect(
      screen
        .getByRole("button", { name: "Initiative work only" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("toggling writes the param and toggling back to default leaves a bare URL", async () => {
    const user = userEvent.setup();
    renderRoadmap();
    await screen.findByText("Replication continuity");
    expect(currentUrl()).toBe("/governance/roadmap");
    const hideStale = screen.getByRole("button", { name: "Hide stale scopes" });
    await user.click(hideStale);
    expect(currentUrl()).toBe("/governance/roadmap?show=stale");
    await user.click(hideStale);
    // Back at the declared default => nothing to say, so the URL says nothing.
    expect(currentUrl()).toBe("/governance/roadmap");
  });

  it("the group param round-trips the grouped view", async () => {
    const user = userEvent.setup();
    renderRoadmap("/governance/roadmap?group=1");
    await screen.findByRole("heading", { name: "snowlinedev" });
    expect(
      screen.getByRole("button", { name: "By org" }).getAttribute("aria-pressed"),
    ).toBe("true");
    // Flat is the default, so selecting it clears the param rather than
    // writing ?group=0.
    await user.click(screen.getByRole("button", { name: "Flat" }));
    expect(currentUrl()).toBe("/governance/roadmap");
    await user.click(screen.getByRole("button", { name: "By org" }));
    expect(currentUrl()).toBe("/governance/roadmap?group=1");
    await screen.findByRole("heading", { name: "snowlinedev" });
  });

  it("an unknown facet key degrades silently and leaves the toggles working", async () => {
    const user = userEvent.setup();
    renderRoadmap("/governance/roadmap?show=no-such-facet");
    await screen.findByText("Replication continuity");
    // No declared facet matches, so the board renders its plain defaults.
    expect(screen.queryByText("Stale exploration")).toBeNull();
    const hideStale = screen.getByRole("button", { name: "Hide stale scopes" });
    expect(hideStale.getAttribute("aria-pressed")).toBe("true");
    // The shell rewrites only the params it owns; the first toggle recomputes
    // show/hide from the DECLARED facets, normalizing the stray key away.
    await user.click(hideStale);
    await screen.findByText("Stale exploration");
    expect(currentUrl()).toBe("/governance/roadmap?show=stale");
  });

  it("refreshing the page keeps the filters applied", async () => {
    const user = userEvent.setup();
    const first = renderRoadmap();
    await screen.findByText("Replication continuity");
    await user.click(screen.getByRole("button", { name: "Hide stale scopes" }));
    await user.click(screen.getByRole("button", { name: "By org" }));
    const url = currentUrl();
    expect(url).toBe("/governance/roadmap?show=stale&group=1");

    // A refresh: tear the app down and mount a brand-new one at that URL.
    first.unmount();
    renderRoadmap(url);
    await screen.findByText("Stale exploration");
    expect(
      screen
        .getByRole("button", { name: "Hide stale scopes" })
        .getAttribute("aria-pressed"),
    ).toBe("false");
    expect(
      screen.getByRole("button", { name: "By org" }).getAttribute("aria-pressed"),
    ).toBe("true");
    await screen.findByRole("heading", { name: "acme" });
  });

  it("toggling replaces history, so Back leaves the page instead of undoing a toggle", async () => {
    const user = userEvent.setup();
    render(
      // Arrived at the board FROM Today, the way a person would.
      <MemoryRouter initialEntries={["/", "/governance/roadmap"]} initialIndex={1}>
        <App />
        <LocationProbe />
        <BackButton />
      </MemoryRouter>,
    );
    await screen.findByText("Replication continuity");
    await user.click(screen.getByRole("button", { name: "Hide stale scopes" }));
    await user.click(screen.getByRole("button", { name: "By org" }));
    expect(currentUrl()).toBe("/governance/roadmap?show=stale&group=1");
    // Two toggles pushed NOTHING, so one Back crosses the route boundary.
    await user.click(screen.getByRole("button", { name: "GO BACK" }));
    await waitFor(() => expect(currentUrl()).toBe("/"));
  });
});

describe("board kind: registered nav omission", () => {
  it("a nav:false board page is not listed in the nav", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    const nav = screen.getByRole("navigation", { name: "Main" });
    await within(nav).findByText("governance");
    // By HREF, not by name: the fixture page is titled "Roadmap", which is
    // also the platform's Roadmap SECTION (dashboard-ia.md §3) — the page's
    // own plugin-namespaced link is the thing that must be absent.
    expect(
      within(nav)
        .getAllByRole("link")
        .map((a) => a.getAttribute("href")),
    ).not.toContain("/governance/roadmap");
  });
});

/** ui-shell.md §4.2a "The drawer": UNPLACED work (the pm placement queue) is a
 * dockable fly-out BESIDE the board, not a section of the tree
 * (snowlinedev/snowline-pm decision b9116311). The shell half: the toggle, the
 * overlay/docked panel, the shared node renderer, and the `?drawer=` view
 * state. */
describe("board kind: drawer", () => {
  it("renders exactly as before, with no toggle, when the payload declares none", async () => {
    renderRoadmap("/governance/roadmap-no-drawer");
    await screen.findByText("Placed node");
    // The facet toggle still renders — only the drawer affordance is absent.
    expect(screen.getByRole("button", { name: "Hide stale scopes" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Placement queue/ })).toBeNull();
    // Not even a closed panel in the DOM, and no two-column wrapper.
    expect(document.querySelector(".board-drawer")).toBeNull();
    expect(document.querySelector(".board-body")).toBeNull();
  });

  it("shows the title, the visible-node count and the plugin's count_label", async () => {
    renderRoadmap();
    await screen.findByText("Replication continuity");
    const toggle = drawerToggle();
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    // aria-controls stays a VALID idref even closed: the panel is mounted and
    // `hidden`, the same way a collapsed subtree is.
    const panel = drawerPanel();
    expect(toggle.getAttribute("aria-controls")).toBe(panel.getAttribute("id"));
    expect(panel.hidden).toBe(true);
    // Closed means closed to AT too — no heading, no controls in the a11y tree.
    expect(screen.queryByRole("heading", { name: "Placement queue" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Dock" })).toBeNull();
  });

  it("opens as an overlay on click and closes again, restoring focus to the toggle", async () => {
    const user = userEvent.setup();
    renderRoadmap();
    await screen.findByText("Replication continuity");
    const toggle = drawerToggle();

    await user.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const panel = drawerPanel();
    expect(panel.hidden).toBe(false);
    // Overlay, not docked: the panel's own class and the board root's.
    expect(panel.className).toContain("board-drawer-overlay");
    expect(boardRoot().className).not.toContain("board-docked");
    // A labelled region named by its own heading, and focus moved INTO it.
    expect(screen.getByRole("heading", { name: "Placement queue" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Placement queue" })).toBe(panel);
    expect(document.activeElement).toBe(panel);

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(panel.hidden).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    // Focus returns to the control that opened it, never to the document.
    expect(document.activeElement).toBe(toggle);
  });

  it("Escape closes the overlay", async () => {
    const user = userEvent.setup();
    renderRoadmap();
    await screen.findByText("Replication continuity");
    await user.click(drawerToggle());
    expect(drawerPanel().hidden).toBe(false);
    await user.keyboard("{Escape}");
    expect(drawerPanel().hidden).toBe(true);
    expect(currentUrl()).toBe("/governance/roadmap");
  });

  it("Escape leaves the DOCKED panel alone — docked is a chosen layout, not an overlay", async () => {
    const user = userEvent.setup();
    renderRoadmap("/governance/roadmap?drawer=docked");
    await screen.findByText("Replication continuity");
    expect(drawerPanel().hidden).toBe(false);
    await user.keyboard("{Escape}");
    expect(drawerPanel().hidden).toBe(false);
    expect(currentUrl()).toBe("/governance/roadmap?drawer=docked");
  });

  it("dock/undock flips the layout class and the button's own label", async () => {
    const user = userEvent.setup();
    renderRoadmap();
    await screen.findByText("Replication continuity");
    await user.click(drawerToggle());
    expect(boardRoot().className).not.toContain("board-docked");

    await user.click(screen.getByRole("button", { name: "Dock" }));
    expect(boardRoot().className).toContain("board-docked");
    expect(drawerPanel().className).toContain("board-drawer-docked");
    // The STATE is in visible text, never color or position alone.
    expect(screen.queryByRole("button", { name: "Dock" })).toBeNull();
    const undock = screen.getByRole("button", { name: "Undock" });
    // Still expanded, and the docked layout is serialized.
    expect(drawerToggle().getAttribute("aria-expanded")).toBe("true");
    expect(currentUrl()).toBe("/governance/roadmap?drawer=docked");

    await user.click(undock);
    expect(boardRoot().className).not.toContain("board-docked");
    expect(drawerPanel().className).toContain("board-drawer-overlay");
    expect(screen.getByRole("button", { name: "Dock" })).toBeTruthy();
    expect(currentUrl()).toBe("/governance/roadmap?drawer=open");
  });

  it("renders drawer nodes with the tree's node renderer, unnumbered", async () => {
    const user = userEvent.setup();
    renderRoadmap();
    await screen.findByText("Replication continuity");
    await user.click(drawerToggle());
    const panel = drawerPanel();

    // Chip, badge and a plugin-relative href all go through BoardNodeRow, so
    // the href is re-namespaced under the plugin exactly as the tree's is.
    expect(within(panel).getByText("Triage inbound")).toBeTruthy();
    expect(within(panel).getByText("NEW")).toBeTruthy();
    expect(within(panel).getByText("snowlinedev/snowline-pm")).toBeTruthy();
    expect(
      within(panel)
        .getByRole("link", { name: "Queued with a link" })
        .getAttribute("href"),
    ).toBe("/governance/roadmap/item-queued");
    // Flat and UNNUMBERED: drawer order is not roadmap order, so a 1-based
    // index would assert an ordering the payload never claimed.
    const list = panel.querySelector(".board-drawer-nodes") as HTMLElement;
    expect(list.tagName).toBe("UL");
    expect(list.querySelector(".board-index")).toBeNull();
    // group_by does not reach inside the drawer.
    expect(panel.querySelector(".board-group-heading")).toBeNull();
  });

  it("facet toggles filter drawer nodes and move the count with them", async () => {
    const user = userEvent.setup();
    renderRoadmap();
    await screen.findByText("Replication continuity");
    await user.click(drawerToggle(2));
    const panel = drawerPanel();
    // `stale` is hidden_by_default, so the stale queue node starts filtered OUT
    // of the drawer exactly as it is out of the tree.
    expect(within(panel).queryByText("Dusty idea")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Hide stale scopes" }));
    // The count on the toggle is the count of VISIBLE drawer nodes.
    expect(drawerToggle(3)).toBeTruthy();
    expect(within(panel).getByText("Dusty idea")).toBeTruthy();
    // The facet toggle keeps the drawer's own view state, and the drawer keeps
    // the facet's — both are params the board owns. (Param ORDER is whatever
    // the surviving URLSearchParams gives: the shell rewrites values, never
    // re-sorts a query string it only partly owns.)
    expect(currentUrl()).toBe("/governance/roadmap?drawer=open&show=stale");

    await user.click(screen.getByRole("button", { name: "Hide stale scopes" }));
    expect(drawerToggle(2)).toBeTruthy();
    expect(within(panel).queryByText("Dusty idea")).toBeNull();
    expect(currentUrl()).toBe("/governance/roadmap?drawer=open");
  });

  it("?drawer=docked renders docked on load and closing takes the URL back to bare", async () => {
    const user = userEvent.setup();
    renderRoadmap("/governance/roadmap?drawer=docked");
    await screen.findByText("Replication continuity");
    expect(boardRoot().className).toContain("board-docked");
    expect(drawerPanel().hidden).toBe(false);
    expect(screen.getByRole("button", { name: "Undock" })).toBeTruthy();
    expect(drawerToggle().getAttribute("aria-expanded")).toBe("true");
    // A deep link must not STEAL focus on first paint — it isn't an "open".
    expect(document.activeElement).not.toBe(drawerPanel());

    await user.click(screen.getByRole("button", { name: "Close" }));
    // Closed is the default, so it writes nothing at all.
    expect(currentUrl()).toBe("/governance/roadmap");
    expect(drawerPanel().hidden).toBe(true);
  });

  it("?drawer=open opens the overlay on load, and the toggle closes it", async () => {
    const user = userEvent.setup();
    renderRoadmap("/governance/roadmap?drawer=open");
    await screen.findByText("Replication continuity");
    expect(drawerPanel().hidden).toBe(false);
    expect(boardRoot().className).not.toContain("board-docked");
    await user.click(drawerToggle());
    expect(drawerPanel().hidden).toBe(true);
    expect(currentUrl()).toBe("/governance/roadmap");
  });

  it("an unreadable ?drawer= value degrades to closed and the next toggle drops it", async () => {
    const user = userEvent.setup();
    renderRoadmap("/governance/roadmap?drawer=sideways");
    await screen.findByText("Replication continuity");
    expect(drawerPanel().hidden).toBe(true);
    // `drawer` is a param the shell OWNS, so the first toggle normalizes it.
    await user.click(screen.getByRole("button", { name: "Hide stale scopes" }));
    expect(currentUrl()).toBe("/governance/roadmap?show=stale");
  });

  it("a drained queue still gets its toggle, and the panel says so", async () => {
    const user = userEvent.setup();
    renderRoadmap("/governance/roadmap-drawer-empty");
    await screen.findByText("Placed node");
    // Count 0, and no count_label declared — just the number.
    const toggle = screen.getByRole("button", { name: "Placement queue · 0" });
    await user.click(toggle);
    expect(screen.getByText("Nothing waiting for placement.")).toBeTruthy();
  });

  it("fails the WHOLE board visible on a malformed drawer", async () => {
    render(
      <MemoryRouter initialEntries={["/governance/roadmap-bad-drawer"]}>
        <App />
      </MemoryRouter>,
    );
    const error = await screen.findByRole("alert");
    expect(error.textContent).toContain("/ui-api/pages/roadmap-bad-drawer");
    // Not the tree beside a silently-dropped queue — the whole card fails.
    expect(screen.queryByText("Placed node")).toBeNull();
  });
});

/** axe over the drawer in BOTH of its states (ACCESSIBILITY.md: semantics,
 * roles, names, structure). a11y.test.tsx audits the board with the drawer
 * CLOSED as part of its per-page sweep; the open and docked states are
 * conditional DOM this board test owns, so they are audited here rather than
 * leaving two thirds of the feature unaudited. */
describe("board kind: drawer accessibility", () => {
  for (const [state, entry] of [
    ["open (overlay)", "/governance/roadmap?drawer=open"],
    ["docked", "/governance/roadmap?drawer=docked"],
  ] as const) {
    it(`has no axe violations with the drawer ${state}`, async () => {
      const { container } = render(
        <MemoryRouter initialEntries={[entry]}>
          <App />
        </MemoryRouter>,
      );
      await screen.findByRole("heading", { name: "Placement queue" });
      await waitFor(() => {
        expect(screen.queryAllByText(/Loading…/)).toHaveLength(0);
        expect(screen.queryAllByText(/Failed to load/)).toHaveLength(0);
      });
      // Contrast is the token validator's job (jsdom applies no styling).
      const results = await axe.run(container, {
        rules: { "color-contrast": { enabled: false } },
      });
      expect(results.violations.map((v) => `${v.id}: ${v.nodes[0]?.html}`)).toEqual([]);
    });
  }
});
