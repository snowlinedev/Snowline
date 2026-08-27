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
